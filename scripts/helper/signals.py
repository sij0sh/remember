"""Target-scoped recorded signals: leads with refs, never verdicts (D67-D73, D148-D151).

Six kinds over the scheduled scan's operations for one file or symbol: failed_exchange,
correction_candidate, checkpoint_gap, repeated_read, failure_after_fix, verification_gap.
Python grouping over provider rows; nothing is persisted. Repeated reads do not prove waste;
errors do not prove reusable pitfalls; a correction is matched by phrasing only; a failure
after a fix does not prove the fix failed; a verification gap is absence in scanned scope.
"""
import hashlib
import json
import re

from . import error_signatures, reconciliation, trace
from .episode_links import episode_for
from .file_observations import read_pairs
from .investigate import _key, locator, noise
from .message_segments import prose, visible
from .profiles import PROFILES
from .projection import human
from .reader import message_graph
from .target_history import CORRECTION

KINDS = ("failed_exchange", "correction_candidate", "checkpoint_gap", "repeated_read", "failure_after_fix",
         "verification_gap")
VERIFY_CLAIM = re.compile(r"\b(tests? pass(es|ed)?|all (tests )?green|verified|works now)\b", re.I)
EXPECTED_FAIL = re.compile(r"expect(ed|s)? (it )?to fail|xfail|negative test", re.I)
PUBLISH = re.compile(r"(?:^|[.!?;:]\s+|\b(?:please|then|and|now|to)\s+)(push|publish|release|deploy)\b", re.I)
MAX_REFS = 5
CHECKPOINT_HARNESSES = frozenset(name for name, profile in PROFILES.items()
                                 if '"emit": "checkpoint"' in json.dumps(profile))


def plan():
    return [{"step": "resolve", "detail": "one file or symbol target; ambiguity returns candidates"},
            {"step": "scan", "detail": "scheduled recording scan; invocation totals across harnesses"},
            {"step": "select", "detail": "operations on the exact repository path (symbol: mapped edit events)"},
            {"step": "group", "detail": ", ".join(KINDS)},
            {"step": "order", "detail": "round-robin across kinds, newest first within a kind"}]


def _ref(ref):
    return locator(ref) if isinstance(ref, dict) else None


def _unique(rows):
    """One row per operation; association rows of the same call share an outcome."""
    seen, out = set(), []
    for row in rows:
        if row.get("operation_id") not in seen:
            seen.add(row.get("operation_id"))
            out.append(row)
    return out


def _row(kind, anchor, excerpt, count, tools, ref, recording, **fields):
    return {"section": "signal", "id": f"{kind}:{ref}", "categories": [kind], "excerpt": excerpt,
            "count": count, "tools": sorted(t for t in tools if t), "ref": ref,
            "session": recording.get("session") if recording else None,
            "timestamp": anchor.get("timestamp") if anchor else None, **fields}


def failed_exchanges(ops, files, by_ref):
    groups = {}
    for op in ops:
        groups.setdefault((op.get("recording_id"), op.get("lane"), _key(op.get("request_ref"))
                           or op.get("turn")), []).append(op)
    rows, skipped = [], 0
    for group in groups.values():
        failures = []
        for op in group:
            if op.get("outcome") != "failed":
                continue
            result = by_ref.get(_key(op.get("result_ref")))
            detail = visible(result.get("text") if result else "")
            if not detail and op.get("exit_code") == 1:
                skipped += 1  # grep/diff style "no match" (investigate rule)
                continue
            failures.append((op, detail))
        if not failures:
            continue
        op, detail = failures[0]
        last = max(group, key=lambda o: o.get("order") or 0).get("outcome") or "unknown"
        ref = _ref(op.get("result_ref") or op.get("call_ref"))
        rows.append(_row("failed_exchange", op, f"{op.get('tool') or op.get('action')} failed: "
                         f"{detail[:160] or 'no output'}", len({o.get('operation_id') for o, _ in failures}),
                         {o.get("tool") for o, _ in failures}, ref, files.get(op.get("recording_id")),
                         last_outcome=last, trace=_ref(op.get("request_ref")) or ref,
                         meaning="recorded failures in one exchange; not a confirmed pitfall or cause"))
    return rows, skipped


def corrections(ops, files, by_ref):
    exchanges = {}
    for op in ops:
        request = by_ref.get(_key(op.get("request_ref")))
        if request is not None:
            exchanges[(op.get("recording_id"), _key(request["ref"]))] = (request, op)
    rows = []
    for (file, _), (request, op) in exchanges.items():
        recording = files.get(file)
        if recording is None:
            continue
        lane, after = request.get("lane"), request.get("order") or 0
        prior, following = None, None
        for event in recording["events"]:
            if event.get("lane") != lane or (event.get("order") or 0) <= after:
                continue
            if event["kind"] == "assistant_message" and prose(event):
                prior = event
            elif human(event):
                following = event
                break
        if following is None:
            continue
        text = prose(following)
        if noise(text) or not CORRECTION.search(text[:300]):
            continue
        rows.append(_row("correction_candidate", following, text[:240], 1, {op.get("tool")},
                         _ref(following["ref"]), recording, corrects=_ref(prior["ref"]) if prior else None,
                         after_request=_ref(request["ref"]), basis="phrasing",
                         meaning="next human message after an exchange on the target; phrasing match only"))
    return rows


def checkpoint_gaps(ops, files, checkpoints):
    rows, unresolved = [], 0
    by_recording = {}
    for op in ops:
        by_recording.setdefault(op.get("recording_id"), []).append(op)
    for checkpoint in checkpoints:
        mine, recording = by_recording.get(checkpoint.get("recording_id")), files.get(checkpoint.get("recording_id"))
        if not mine or recording is None:
            continue
        graph = message_graph(recording["events"])
        gap = trace._gap(recording, checkpoint, graph)
        if gap["status"] != "observed":
            unresolved += 1
            continue
        events = {e.get("event_id"): e for e in recording["events"]}
        publication = events.get(checkpoint.get("publication_event_id"))
        chain, _ = graph["chain"](trace._mid(publication))
        path = set(chain) | {trace._mid(publication)}
        after = [op for op in mine if gap["lower_order"] < (op.get("order") or 0) < gap["upper_order"]
                 and op.get("call_event_id") in events and trace._mid(events[op["call_event_id"]]) in path]
        if not after:
            continue
        rows.append(_row("checkpoint_gap", checkpoint, f"{len(after)} target operations after the checkpoint's "
                         "coverage boundary, before its publication", len(after), {o.get("tool") for o in after},
                         _ref(checkpoint.get("publication_ref")), recording,
                         boundary_status=checkpoint.get("boundary_status"), ops_after=len(after),
                         refs=[_ref(o.get("call_ref")) for o in after[:MAX_REFS]],
                         meaning="the checkpoint claim does not cover this target work"))
    return rows, unresolved


def repeated_reads(ops, files, observations=()):
    ids = {op.get("operation_id") for op in ops}
    states = {}
    for _, later, state in read_pairs([o for o in observations if o["operation_id"] in ids]):
        states.setdefault(later["recording_id"], []).append(state)
    sessions = {}
    for op in ops:
        if op.get("action") == "file_read":
            sessions.setdefault(op.get("recording_id"), []).append(op)
    rows = []
    for file, reads in sessions.items():
        if len(reads) < 2:
            continue
        reads.sort(key=lambda o: o.get("order") or 0)
        rows.append(_row("repeated_read", reads[-1], f"{len(reads)} reads of the target in one session",
                         len(reads), {o.get("tool") for o in reads}, _ref(reads[-1].get("call_ref")), files.get(file),
                         reads=len(reads), extra_reads=len(reads) - 1,
                         turns=len({o.get("turn") for o in reads}), first=_ref(reads[0].get("call_ref")),
                         unchanged_pairs=states.get(file, []).count("unchanged"),
                         changed_pairs=states.get(file, []).count("changed"),
                         meaning="unchanged_pairs: same extent, same content hash; not waste"))
    return rows


def order(by_kind):
    """Round-robin across kinds in KINDS order; newest first within a kind; ref tiebreak."""
    pools = [sorted(by_kind.get(kind, []), key=lambda r: r.get("ref") or "")
             for kind in KINDS]
    pools = [sorted(pool, key=lambda r: r.get("timestamp") or "", reverse=True) for pool in pools]
    out = []
    while any(pools):
        for pool in pools:
            if pool:
                out.append(pool.pop(0))
    return out


def views(by_kind, providers, sessions):
    recordings = providers.coverage.get("recordings", {})
    harnesses = {r.get("harness") for r in providers.recordings}
    out = {}
    for kind in KINDS:
        eligible = len(by_kind.get(kind, []))
        if eligible:
            state = "leads_found"
        elif not sessions:
            state = "not_searched"
        elif recordings.get("state") == "unavailable" or (
                kind == "checkpoint_gap" and not harnesses & CHECKPOINT_HARNESSES):
            state = "unavailable"
        else:
            state = "none_found_in_scanned_scope"
        out[kind] = {"state": state, "eligible": eligible}
    return out


def _lane_ops(operations):
    lanes = {}
    for op in _unique(sorted(operations, key=lambda r: (r.get("recording_id") or "", r.get("order") or 0,
                                                         r.get("association") or 0))):
        lanes.setdefault((op.get("recording_id"), op.get("lane")), []).append(op)
    return lanes


def _checks(ops):
    """[(order, value, op)] verified observations, one owner: reconciliation."""
    by_id = {op.get("operation_id"): op for op in ops}
    rows = reconciliation.observations(None, ops, [])
    return [(row.get("order") or 0, row["value"], by_id.get(row.get("operation_id"))) for row in rows
            if row["dimension"] == "verified"]


def _text(by_ref, op, name="result_ref"):
    event = by_ref.get((op.get("recording_id"), _key(op.get(name)))) if op else None
    return visible(event.get("text") or "") if event else ""


def failure_chains(target_eps, lookup, links, lanes, observations, files, by_ref, scope):
    """D149: earlier failure, fix attempt, later failure of the same family; leads, not verdicts."""
    rows, seen, excluded = [], set(), 0
    by_id = (lookup or {}).get("by_id", {})
    for link in links:
        if link["relation"] != "possible_regression" or not ({link["source"], link["target"]} & target_eps):
            continue
        earlier, later = by_id.get(link["source"]), by_id.get(link["target"])
        if earlier is None or later is None:
            continue
        for family, first_order, first in earlier.failures:
            fixes = [op for op in earlier.ops if op.get("action") == "file_edit" and op.get("outcome") == "succeeded"
                     and (op.get("order") or 0) > first_order]
            again = next(((o, op) for fam, o, op in later.failures if fam == family), None)
            if not fixes or again is None:
                continue
            later_order, second = again
            chain = "chain:" + hashlib.sha256(json.dumps([first.get("operation_id"), second.get("operation_id")])
                                               .encode()).hexdigest()[:16]
            if chain in seen:
                continue
            seen.add(chain)
            first_text, second_text = _text(by_ref, first), _text(by_ref, second)
            duplicate = (first.get("recording_id") != second.get("recording_id") and first_text == second_text
                         and earlier.request.get("independent_session") == later.request.get("independent_session"))
            expected = any(EXPECTED_FAIL.search(t or "") for t in (
                first.get("command"), second.get("command"), prose(later.request), prose(earlier.request)))
            if duplicate or expected or not second_text:
                excluded += 1
                continue
            fix = fixes[-1]
            path = fix.get("path")
            lane = lanes.get((second.get("recording_id"), second.get("lane")), [])
            same_lane = fix.get("recording_id") == second.get("recording_id") and fix.get("lane") == second.get(
                "lane") and not any(op.get("action") == "file_edit" and op.get("path") == path and op.get(
                    "outcome") == "succeeded" and (fix.get("order") or 0) < (op.get("order") or 0) < later_order
                    for op in lane)
            post = next((o["content_sha256"] for o in observations if o["operation_id"] == fix.get("operation_id")
                         and o["completeness"] == "complete"), None)
            identical = post and any(
                o["kind"] == "read_result" and o["completeness"] == "complete" and o["path"] == path
                and o["content_sha256"] == post and o["recording_id"] == second.get("recording_id")
                and (o.get("order") or 0) < later_order
                and (o["recording_id"] != fix.get("recording_id") or (o.get("order") or 0) > (fix.get("order") or 0))
                for o in observations)
            exposure = "same_lane_after_fix" if same_lane else "content_identity" if identical else "unknown"
            exact = {e for e, _ in error_signatures.signature(first_text)} & {
                e for e, _ in error_signatures.signature(second_text)}
            verification = [_ref(op.get("call_ref")) for op in earlier.ops + later.ops
                            if op.get("action") == "command" and "test" in reconciliation.command_kinds(
                                op.get("command")) and (fix.get("order") or 0) < (op.get("order") or 0)
                            and (op.get("recording_id") != second.get("recording_id")
                                 or (op.get("order") or 0) < later_order)][:MAX_REFS]
            subtype = "failure_after_fix" if exposure != "unknown" else "possible_failure_after_fix"
            row = _row("failure_after_fix", second, f"{family[:120]} failed again after a fix attempt", 1,
                       {first.get("tool"), second.get("tool")}, _ref(second.get("result_ref") or second.get("call_ref")),
                       files.get(second.get("recording_id")), subtype=subtype, chain_id=chain, exposure=exposure,
                       basis="error_family_exact" if exact else "error_family", family=family,
                       initial=_ref(first.get("result_ref") or first.get("call_ref")),
                       fixes=[_ref(op.get("call_ref")) for op in fixes][-MAX_REFS:], later=_ref(second.get("result_ref")),
                       verification=verification, refs=[_ref(first.get("result_ref")), _ref(fix.get("call_ref")),
                                                         _ref(second.get("result_ref"))],
                       rules=["possible_regression_link", "fix_attempt_D145", "same_error_family"],
                       uncertainty=None if exposure != "unknown" else "fix not shown to precede the later failure "
                       "on the same lane or file content", scope=scope,
                       meaning="same error family after a recorded fix attempt; not proof the fix failed")
            row["id"] = f"failure_after_fix:{chain}"
            rows.append(row)
    return rows, excluded


def verification_gaps(target_eps, lookup, lanes, path, files, scope):
    """D150 subtypes per target episode; absence is reported in scope, never as a verdict."""
    rows = []
    by_id = (lookup or {}).get("by_id", {})
    def emit(subtype, ep, anchor, ref, excerpt, rules, missing=None, count=1, **fields):
        reason = "truncated" if not ep.recording.get("scan_complete", True) else scope["reason"]
        row = _row("verification_gap", anchor, excerpt, count, {anchor.get("tool")} if anchor.get("tool") else set(),
                   ref, files.get(ep.recording["file"]), subtype=subtype, rules=rules, episode_id=ep.id,
                   scope={**scope, "reason": reason}, missing={missing: "not_observed_in_scope"} if missing else {},
                   meaning="a check or publication not observed in scanned scope; not proof it did not happen",
                   **fields)
        row["id"] = f"verification_gap:{subtype}:{ref}"
        rows.append(row)
    for episode_id in sorted(target_eps):
        ep = by_id.get(episode_id)
        if ep is None:
            continue
        checks = _checks(ep.ops)
        lane = lanes.get((ep.recording["file"], ep.lane), [])
        lane_checks = _checks(lane)
        claims = [e for e in ep.events if e.get("kind") == "assistant_message" and VERIFY_CLAIM.search(prose(e))]
        if claims and not checks:
            emit("claim_without_observed_check", ep, claims[0], _ref(claims[0]["ref"]), prose(claims[0])[:200],
                 ["VERIFY_CLAIM", "no_verified_observation"], "verified")
        # One row per episode and subtype; count and refs carry the instances (D114, D151).
        failed = [op for order, value, op in checks if value == "test_run_failed"
                  and not any(o > order and v == "test_run_passed" for o, v, _ in lane_checks)]
        if failed:
            emit("check_failed", ep, failed[-1], _ref(failed[-1].get("result_ref") or failed[-1].get("call_ref")),
                 f"test failed: {(failed[-1].get('command') or '')[:160]}",
                 ["test_run_failed", "no_later_success_on_lane"], "verified", basis="recorded_test_result",
                 count=len(failed), refs=[_ref(op.get("result_ref")) for op in failed][-MAX_REFS:])
        compound = [op for _, value, op in checks if value == "test_run_unattributed"]
        if compound:
            emit("check_scope_unknown", ep, compound[-1], _ref(compound[-1].get("call_ref")),
                 f"compound test command: {(compound[-1].get('command') or '')[:160]}", ["compound_command_D115"],
                 uncertainty="aggregate exit status does not belong to the test", count=len(compound),
                 refs=[_ref(op.get("call_ref")) for op in compound][-MAX_REFS:])
        passed = [o for o, v, _ in checks if v == "test_run_passed"]
        if passed:
            edits = [op for op in lane if op.get("action") == "file_edit" and op.get("outcome") == "succeeded"
                     and op.get("path") == path and (op.get("order") or 0) > max(passed)]
            later = edits and not any(o > (edits[-1].get("order") or 0) and v == "test_run_passed"
                                      for o, v, _ in lane_checks)
            if later:
                emit("check_predates_change", ep, edits[-1], _ref(edits[-1].get("call_ref")),
                     f"{path} edited after the last passing check", ["test_run_passed", "later_edit_on_lane",
                                                                    "no_later_success_on_lane"], "verified",
                     check=next(_ref(op.get("call_ref")) for o, v, op in checks if o == max(passed)))
        request = prose(ep.request)
        if PUBLISH.search(request):
            pushes = [row for row in reconciliation.observations(None, ep.ops, [])
                      if row["dimension"] == "pushed" and row["value"] == "push_succeeded"]
            if not pushes:
                emit("publication_unconfirmed", ep, ep.request, _ref(ep.request["ref"]), request[:200],
                     ["PUBLISH_imperative", "no_push_succeeded"], "pushed")
    return rows


def collect(providers, selected):
    """(rows in delivery order, by_kind, selection coverage)."""
    providers.load({"checkpoints", "file_observations", "episodes"})
    files = {r["file"]: r for r in providers.recordings}
    by_ref = {_key(e["ref"]): e for r in providers.recordings for e in r["events"]}
    ops = _unique(sorted(selected, key=lambda r: (r.get("recording_id") or "", r.get("order") or 0)))
    failed, empty_exits = failed_exchanges(ops, files, by_ref)
    gaps, unresolved = checkpoint_gaps(ops, files, providers.tables.get("checkpoints", []))
    observations = providers.tables.get("file_observations", [])
    lookup, lanes = providers.episodes, _lane_ops(providers.tables.get("operations", []))
    target_eps = {ep for ep in (episode_for(lookup, op.get("recording_id"), op.get("request_ref")) for op in ops) if ep}
    complete = all(providers.coverage.get(name, {}).get("complete") is True for name in ("recordings", "operations"))
    scope = {"episodes": len(target_eps), "lanes": len({(op.get("recording_id"), op.get("lane")) for op in ops}),
             "reason": "complete" if complete else "truncated"}
    by_file = {(r["file"], _key(e["ref"])): e for r in providers.recordings for e in r["events"]}
    chains, chains_excluded = failure_chains(target_eps, lookup, providers.tables.get("episode_links", []), lanes,
                                             observations, files, by_file, scope)
    path = next((op.get("path") for op in ops if op.get("path")), None)
    by_kind = {"failed_exchange": failed, "correction_candidate": corrections(ops, files, by_ref),
               "checkpoint_gap": gaps, "repeated_read": repeated_reads(ops, files, observations),
               "failure_after_fix": chains,
               "verification_gap": verification_gaps(target_eps, lookup, lanes, path, files, scope)}
    coverage = {"operations": len(ops), "empty_nonzero_exits_skipped": empty_exits,
                "checkpoints_unresolved": unresolved, "episodes": len(target_eps),
                "chains": len({row["chain_id"] for row in chains}), "chains_excluded": chains_excluded,
                "meaning": "signals are leads with refs; they do not establish waste, cause or fix"}
    return order(by_kind), by_kind, coverage
