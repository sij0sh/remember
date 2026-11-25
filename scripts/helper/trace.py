"""Branch-aware trace episodes over shared recording, operation, checkpoint and Git facts.

A trace answers: what was requested, what was attempted, what was observed, what a
summary claimed and which evidence its coverage did not include. It does not decide
that a task is complete, and it never lets one branch borrow another branch's
request, result or state. All projections are invocation-local.
"""
import json
import re

from . import cite, continuations, episode_links, message_segments, reconciliation
from .graph_store import digest
from .reader import message_graph

MAX_DEEPEN = 2
MAX_WIDEN = 3
MAX_RELATED = 5
MAX_LINKS = 8
SHA = re.compile(r"(?:git:)?[0-9a-fA-F]{40}")


def _key(ref):
    return (ref.get("offset"), ref.get("item", 0)) if isinstance(ref, dict) else None


def _find(recording, ref):
    wanted = _key(ref)
    if not wanted or not recording:
        return None
    return next((event for event in recording["events"]
                 if (event["ref"]["offset"], event["ref"].get("item", 0)) == wanted), None)


def _mid(event):
    return event.get("message_id") or event.get("external_message_id") or \
        f"{event['ref']['offset']}:{event['ref'].get('item', 0)}"


def _order(event):
    return event.get("order") or 0


def _request(target, recording, graph):
    """(request event, basis) for the anchor; unsupported lineage is explicitly unassigned."""
    if target.get("kind") == "user_message":
        if target.get("request_origin") == "human":
            return target, "self"
        return target, "self_" + (target.get("request_origin") or "unknown")
    chosen = _find(recording, target.get("request_ref"))
    if chosen is None or chosen.get("kind") != "user_message":
        return None, "unresolved"
    basis = target.get("request_basis") or "recorded_ancestry"
    if basis not in ("recorded_ancestry", "recorded_turn"):
        return None, f"unresolved_{basis}"
    ancestor, status = graph["is_ancestor"](_mid(chosen), _mid(target))
    if status in ("missing", "cycle", "ambiguous"):
        return None, f"unresolved_ancestry_{status}"
    if not ancestor and chosen is not target:
        return None, "request_not_ancestor"
    return chosen, basis


def _exchange(recording, target, request, graph):
    """Events in the anchor's exchange, or an explicitly unassigned ancestry fragment."""
    events = recording["events"]
    if request is not None:
        wanted = _key(request.get("ref"))
        selected = [event for event in events if _key(event.get("request_ref")) == wanted]
        if not any(event is request for event in selected):
            selected.append(request)
        return selected, "human_exchange" if request.get("request_origin") == "human" else "delegated_exchange"
    target_id = _mid(target)
    ancestors = set(graph["chain"](target_id)[0])
    selected, seen = [], set()
    for event in events:
        message = _mid(event)
        if message == target_id or message in ancestors or target_id in graph["chain"](message)[0]:
            identity = (event["ref"]["offset"], event["ref"].get("item", 0))
            if identity not in seen:
                seen.add(identity)
                selected.append(event)
    return selected, "unassigned_fragment"


def _lanes(selected, request, graph):
    groups = {}
    for event in selected:
        groups.setdefault(event.get("branch") or _mid(event), []).append(event)
    request_mid = _mid(request) if request else None
    lanes = []
    for lane, group in groups.items():
        group.sort(key=lambda event: (_order(event), event["ref"].get("item", 0)))
        parent, parent_status = graph["parent"](_mid(group[0]))
        if request_mid and lane == request_mid:
            role = "request"
        elif parent_status == "ok" and request_mid and parent == request_mid:
            role = "agent_candidate"
        elif parent_status == "ok" and parent:
            role = "fork"
        else:
            role = "branch"
        lanes.append({"lane": lane, "lane_role": role, "events": len(group),
                      "first_order": _order(group[0]), "last_order": _order(group[-1]),
                      "ref": group[0]["ref"],
                      "request_ref": next((event.get("request_ref") for event in group
                                           if event.get("request_ref")), None),
                      "events_by_id": group})
    lanes.sort(key=lambda item: (item["first_order"], item["lane"]))
    # Parallel assistants can both hang directly under one request; the first recorded
    # one is the main agent lane, later siblings are forks rather than merged agents.
    main_agent = next((lane for lane in lanes if lane["lane_role"] == "agent_candidate"), None)
    for lane in lanes:
        if lane["lane_role"] == "agent_candidate":
            lane["lane_role"] = "agent" if lane is main_agent else "fork"
    return lanes


def _gap(recording, checkpoint, graph, witnesses=()):
    """Covered-boundary to publication interval on the publication's supported branch."""
    publication = next((event for event in recording["events"]
                        if event.get("event_id") == checkpoint.get("publication_event_id")), None)
    if publication is None:
        return {"status": "publication_not_found", "messages": None, "operations": None, "refs": []}
    if checkpoint.get("boundary_status") != "resolved" or checkpoint.get("boundary_order") is None:
        return {"status": checkpoint.get("boundary_status") or "unresolved", "messages": None,
                "operations": None, "refs": []}
    chain, status = graph["chain"](_mid(publication))
    if status in ("missing", "cycle", "ambiguous"):
        return {"status": f"unresolved_ancestry_{status}", "messages": None, "operations": None, "refs": []}
    coverage_end = checkpoint.get("coverage_end_id")
    if coverage_end not in chain:
        return {"status": "boundary_not_on_publication_branch", "messages": None, "operations": None, "refs": []}
    path = set(chain) | {_mid(publication)}
    path_branches = {event.get("branch") for event in recording["events"] if _mid(event) in path}
    lower, upper = checkpoint["boundary_order"], checkpoint.get("order") or 0
    events = [event for event in recording["events"]
              if _mid(event) in path and lower < _order(event) < upper]
    messages = {_mid(event) for event in events}
    operations = {event.get("event_id") for event in events
                  if event.get("kind") in ("tool_call", "command", "file_read", "file_edit")}
    commits = [{"witness_id": item.get("witness_id"), "sha": item.get("sha"),
                "recorded_sha": item.get("recorded_sha"), "confidence": item.get("confidence"),
                "failed": item.get("failed"), "branch": item.get("branch"),
                "order": item.get("order")}
               for item in witnesses
               if item.get("order") is not None and lower < item["order"] < upper
               and item.get("branch") in path_branches
               and (item.get("sha") or item.get("confidence") == "recorded")]
    return {"status": "observed", "messages": len(messages), "operations": len(operations),
            "refs": [event["ref"] for event in events[:2]], "commits": commits[:4],
            "lower_order": lower, "upper_order": upper}


def _later_gap(selected, checkpoint):
    if checkpoint.get("boundary_status") != "resolved":
        return {"status": "unresolved_boundary", "messages": None, "operations": None, "refs": []}
    lane, after = checkpoint.get("lane"), checkpoint.get("order") or 0
    events = [event for event in selected if event.get("lane") == lane and _order(event) > after]
    messages = {_mid(event) for event in events}
    operations = {event.get("event_id") for event in events
                  if event.get("kind") in ("tool_call", "command", "file_read", "file_edit")}
    return {"status": "observed", "messages": len(messages), "operations": len(operations),
            "refs": [event["ref"] for event in events[:2]]}


def _operation_rows(operations, witnesses):
    by_operation, by_witness = {}, {}
    for row in operations:
        by_operation.setdefault(row.get("operation_id"), []).append(row)
    for witness in witnesses:
        by_witness.setdefault(witness.get("operation_id"), []).append(witness)
    rows = []
    for operation_id, group in by_operation.items():
        base = group[0]
        paths, locations = [], []
        for item in group:
            if item.get("path") and item["path"] not in paths:
                paths.append(item["path"])
                locations.append(item.get("path_location"))
        unique = {}
        for witness in by_witness.get(operation_id, []):
            unique[witness.get("witness_id")] = witness
        rows.append({"section": "operation", "operation_id": operation_id,
                     "lane": base.get("lane"), "branch": base.get("branch"), "order": base.get("order"),
                     "timestamp": base.get("timestamp"), "tool": base.get("tool"),
                     "action": base.get("action"), "pairing": base.get("pairing"),
                     "outcome": base.get("outcome"), "interpretation": base.get("interpretation"),
                     "basis": base.get("basis"), "ref": base.get("call_ref") or base.get("result_ref"),
                     "paths": paths[:8],
                     "path_locations": locations[:8], "command": (base.get("command") or "")[:300] or None,
                     "call_ref": base.get("call_ref"),
                     "result_ref": base.get("result_ref"),
                     "commits": [{"witness_id": w.get("witness_id"), "sha": w.get("sha"),
                                  "recorded_sha": w.get("recorded_sha"), "basis": w.get("basis"),
                                  "confidence": w.get("confidence"), "resolution": w.get("resolution"),
                                  "failed": w.get("failed"), "subject": w.get("subject")}
                                 for w in unique.values()]})
    rows.sort(key=lambda row: (row.get("order") or 0, row.get("operation_id") or ""))
    return rows


def _commit_rows(witnesses):
    groups = {}
    for witness in witnesses:
        key = (witness.get("sha") or witness.get("recorded_sha") or witness.get("witness_id"),
               witness.get("confidence"), witness.get("failed"))
        groups.setdefault(key, []).append(witness)
    rows = []
    for (sha, confidence, failed), group in groups.items():
        first = group[0]
        rows.append({"section": "commit", "sha": sha, "recorded_sha": first.get("recorded_sha"),
                     "subject": first.get("subject"), "basis": first.get("basis"),
                     "confidence": confidence, "resolution": first.get("resolution"),
                     "failed": failed, "witnesses": len(group),
                     "recordings": sorted({w.get("recording_id") for w in group if w.get("recording_id")}),
                     "refs": [{"witness_id": w.get("witness_id"), "ref": w.get("ref")}
                              for w in group[:1]]})
    return rows


def episode(recording, target, operations, checkpoints, witnesses):
    """One anchor exchange with branch lanes, checkpoint coverage and dimension evidence."""
    graph = message_graph(recording["events"])
    request, request_basis = _request(target, recording, graph)
    selected, scope = _exchange(recording, target, request, graph)
    lanes = _lanes(selected, request, graph)
    selected_ids = {event.get("event_id") for event in selected}
    operations = [row for row in operations if row.get("recording_id") == recording.get("file")
                  and (row.get("call_event_id") in selected_ids or row.get("result_event_id") in selected_ids)]
    operation_ids = {row.get("operation_id") for row in operations}
    gap_witnesses = [row for row in witnesses if row.get("recording_id") == recording.get("file")]
    witnesses = [row for row in gap_witnesses
                 if row.get("operation_id") in operation_ids or row.get("event_id") in selected_ids]
    frontier = max((_order(event) for event in selected), default=0)
    request_lane = request.get("lane") if request else None
    checkpoints = [row for row in checkpoints if row.get("recording_id") == recording.get("file")
                   and (row.get("publication_event_id") in selected_ids
                        or row.get("boundary_event_id") in selected_ids
                        or (row.get("lane") == request_lane and (row.get("order") or 0) <= frontier))]
    observations = reconciliation.observations(request, operations, witnesses, checkpoints)
    claims = [claim for checkpoint in checkpoints for claim in reconciliation.claims_for(checkpoint)]
    comparisons = [item for checkpoint in checkpoints
                   for item in reconciliation.compare(checkpoint, reconciliation.claims_for(checkpoint), observations)]
    dimensions = reconciliation.summarize(observations, claims)
    rows = []
    if request is not None:
        rows.append({"section": "request", "lane": request.get("lane"), "branch": request.get("branch"),
                     "order": request.get("order"), "origin": request.get("request_origin"),
                     "basis": request_basis, "text": message_segments.prose(request)[:500] or None,
                     "ref": request.get("ref"), "cite": cite.token(request["ref"])})
    rows += _operation_rows(operations, witnesses)
    checkpoint_rows = []
    for checkpoint in sorted(checkpoints, key=lambda row: (row.get("order") or 0, row.get("checkpoint_id") or "")):
        claim_rows = reconciliation.claims_for(checkpoint)
        data = {"section": "checkpoint", "checkpoint_id": checkpoint.get("checkpoint_id"),
                "publication_event_id": checkpoint.get("publication_event_id"),
                "lane": checkpoint.get("lane"), "branch": checkpoint.get("branch"),
                "order": checkpoint.get("order"), "timestamp": checkpoint.get("timestamp"),
                "boundary_status": checkpoint.get("boundary_status"),
                "coverage_end_id": checkpoint.get("coverage_end_id"),
                "boundary_order": checkpoint.get("boundary_order"),
                "boundary_items": checkpoint.get("boundary_items"),
                "version": checkpoint.get("version"), "publication_ref": checkpoint.get("publication_ref"),
                "boundary_ref": checkpoint.get("boundary_ref"),
                "coverage_context": checkpoint.get("publication_event_id") not in selected_ids,
                "claims": claim_rows[:8], "claims_omitted": max(0, len(claim_rows) - 8),
                "coverage_gap": _gap(recording, checkpoint, graph, gap_witnesses),
                "publication_gap": _later_gap(selected, checkpoint)}
        checkpoint_rows.append(data)
    rows += checkpoint_rows
    rows += comparisons
    rows += _commit_rows(witnesses)
    rows += observations
    lineage_gaps = []
    ambiguous = sum(bool(event.get("message_ambiguous")) for event in recording["events"])
    unknown_ancestry = sum(event.get("ancestry_status") in ("missing", "cycle")
                           for event in recording["events"])
    if ambiguous:
        lineage_gaps.append({"section": "gap", "area": "lineage", "reason": "ambiguous_message_ids",
                             "count": ambiguous})
    if unknown_ancestry:
        lineage_gaps.append({"section": "gap", "area": "lineage", "reason": "unresolved_ancestry",
                             "count": unknown_ancestry})
    if request is None:
        lineage_gaps.append({"section": "gap", "area": "request", "reason": request_basis, "count": 1})
    for checkpoint in checkpoints:
        if checkpoint.get("boundary_status") != "resolved":
            lineage_gaps.append({"section": "gap", "area": "checkpoint",
                                 "reason": f"boundary_{checkpoint.get('boundary_status')}",
                                 "ref": checkpoint.get("publication_ref")})
    rows += lineage_gaps
    used_operations = {row.get("operation_id"): row for row in operations}
    unknown_outcomes = {op_id for op_id, row in used_operations.items()
                        if row.get("outcome") not in ("succeeded", "failed")}
    unpaired = {op_id for op_id, row in used_operations.items() if row.get("pairing") != "paired"}
    unresolved_witnesses = [row for row in witnesses
                            if not row.get("failed") and not row.get("sha")]
    unresolved_checkpoints = [row for row in checkpoints if row.get("boundary_status") != "resolved"]
    coverage = {
        "request": {"basis": request_basis, "scope": scope, "resolved": request is not None},
        "lineage": {"scanned_events": len(recording["events"]), "selected_events": len(selected),
                    "lanes": len(lanes), "ambiguous_message_ids": ambiguous,
                    "unresolved_ancestry": unknown_ancestry,
                    "off_branch_boundaries": sum(row.get("boundary_status") == "off_branch"
                                                 for row in checkpoints)},
        "operations": {"complete": not unknown_outcomes and not unpaired,
                       "used": len(used_operations), "unknown_outcomes": len(unknown_outcomes),
                       "unpaired": len(unpaired), "source": "episode-scoped operation facts"},
        "links": {"complete": not unresolved_witnesses, "used": len(witnesses),
                  "unresolved": len(unresolved_witnesses), "source": "episode-scoped witnesses"},
        "checkpoints": {"complete": not unresolved_checkpoints, "used": len(checkpoints),
                        "unresolved_boundaries": len(unresolved_checkpoints),
                        "source": "episode-scoped checkpoint boundaries"},
        "reconciliation": {"complete": True, "comparisons": len(comparisons),
                           "not_comparable": sum(item.get("relation") == "not_comparable"
                                                 for item in comparisons),
                           "unsupported_claims": sum(1 for row in checkpoints
                                                     if row.get("interpretation") != "structured")},
    }
    coverage["episode"] = {"complete": all(coverage[key]["complete"] for key in
                                           ("operations", "links", "checkpoints", "reconciliation")),
                           "operations": len(used_operations), "witnesses": len(witnesses),
                           "checkpoints": len(checkpoints)}
    if coverage["reconciliation"]["unsupported_claims"]:
        coverage["reconciliation"]["complete"] = False
    return {"request": request, "request_basis": request_basis, "scope": scope, "selected": selected,
            "lanes": lanes, "operations": operations, "witnesses": witnesses, "checkpoints": checkpoints,
            "observations": observations, "claims": claims, "comparisons": comparisons,
            "dimensions": dimensions, "rows": rows, "frontier": frontier,
            "coverage": coverage, "graph": graph}


def _trace_ref(base_argv, ref, purpose, effect="expansion"):
    argv = list(base_argv)
    value = json.dumps(ref, separators=(",", ":"))
    replaced = False
    for index in range(1, len(argv)):
        if not argv[index].startswith('-'):
            argv[index] = value
            replaced = True
            break
    if not replaced:
        argv.insert(1, value)
    argv = continuations.replace_option(argv, '--offset', None)
    return continuations.encode(argv, None, purpose, effect)


def _human_requests(recording):
    events = [event for event in recording["events"]
              if event.get("kind") == "user_message" and event.get("request_origin") == "human"
              and (event.get("text") or "").strip()]
    return sorted(events, key=_order)


def _deepen(base_argv, store, data):
    items, seen, omitted = [], set(), 0

    def add(ref, purpose):
        nonlocal omitted
        if not isinstance(ref, dict):
            return
        if len(items) >= MAX_DEEPEN:
            omitted += 1
            return
        identity = _key(ref)
        if identity in seen:
            return
        seen.add(identity)
        argv = ['show', json.dumps(ref, separators=(",", ":")), '--repo', store.root,
                '--cache', str(store.path.parent)]
        items.append(continuations.encode(argv, None, purpose, "expansion"))

    if data["request"] is not None:
        add(data["request"].get("ref"), "Re-read the recorded human request")
    for row in data["rows"]:
        if row.get("section") == "operation":
            add(row.get("call_ref"), "Expand the tool call and its submitted arguments")
            add(row.get("result_ref"), "Expand the recorded tool result")
    for checkpoint in data["checkpoints"]:
        add(checkpoint.get("publication_ref"), "Re-read the original checkpoint claim")
        add(checkpoint.get("boundary_ref"), "Expand the resolved coverage boundary")
    if data["witnesses"]:
        sha = next((w.get("sha") for w in data["witnesses"] if w.get("sha")), None)
        if sha:
            if len(items) < MAX_DEEPEN:
                argv = ['show', sha, '--repo', store.root, '--cache', str(store.path.parent)]
                items.append(continuations.encode(argv, None, "Read the committed diff", "expansion"))
            else:
                omitted += 1
    return items, omitted


def _widen(base_argv, recording, data, extra=()):
    items, seen, omitted = [], set(), 0

    def add(ref, purpose):
        nonlocal omitted
        if not isinstance(ref, dict):
            return
        if len(items) >= MAX_WIDEN:
            omitted += 1
            return
        identity = _key(ref)
        if identity in seen:
            return
        seen.add(identity)
        items.append(_trace_ref(base_argv, ref, purpose))

    request = data["request"]
    if request is not None:
        requests = _human_requests(recording)
        at = next((index for index, event in enumerate(requests) if event is request), None)
        if at is not None:
            if at:
                add(requests[at - 1].get("ref"), "Trace the previous human exchange in this recording")
            if at + 1 < len(requests):
                add(requests[at + 1].get("ref"), "Trace the next human exchange in this recording")
    for lane in data["lanes"]:
        if lane.get("lane_role") == "fork":
            add(lane.get("ref"), "Trace a separate fork lane under the same request")
    for checkpoint in data["checkpoints"]:
        for ref in checkpoint.get("coverage_gap", {}).get("refs") or []:
            add(ref, "Trace evidence after this checkpoint's coverage boundary")
    for ref, purpose in extra:
        add(ref, purpose)
    return items, omitted


def _compact(value):
    """Drop empty counters only; completeness and nonzero evidence stay visible."""
    if isinstance(value, dict):
        found = {}
        for key, item in value.items():
            if key in ("complete", "state", "error", "scope", "source", "meaning"):
                found[key] = item
                continue
            if item in (0, None, [], {}, ""):
                continue
            found[key] = _compact(item)
        return found
    if isinstance(value, list):
        return [_compact(item) for item in value]
    return value


def _package_dimensions(dimensions, scope_note):
    return {"scope": scope_note, **dimensions}


def _reconciliation_summary(data):
    """Small, page-independent view of checkpoint coverage and defensible comparisons."""
    checkpoints = []
    prepared = {row.get("checkpoint_id"): row for row in data["rows"]
                if row.get("section") == "checkpoint"}
    for checkpoint in data["checkpoints"]:
        row = prepared.get(checkpoint.get("checkpoint_id"), {})
        gap, later = row.get("coverage_gap") or {}, row.get("publication_gap") or {}
        checkpoints.append({"checkpoint_id": checkpoint.get("checkpoint_id"),
                            "publication_event_id": checkpoint.get("publication_event_id"),
                            "lane": checkpoint.get("lane"), "order": checkpoint.get("order"),
                            "boundary_status": checkpoint.get("boundary_status"),
                            "boundary_order": checkpoint.get("boundary_order"),
                            "coverage_gap": {"status": gap.get("status"), "messages": gap.get("messages"),
                                             "operations": gap.get("operations"),
                                             "commits": [item.get("sha") or item.get("recorded_sha")
                                                         for item in gap.get("commits") or []]},
                            "publication_gap": {"status": later.get("status"),
                                                "messages": later.get("messages"),
                                                "operations": later.get("operations")}})
    comparisons = [{"relation": row.get("relation"), "reason": row.get("reason"),
                    "dimension": row.get("dimension"), "checkpoint_id": row.get("checkpoint_id"),
                    "claim": row.get("claim"), "observation_value": row.get("observation_value"),
                    "observation_order": row.get("observation_order"),
                    "boundary_order": row.get("boundary_order")}
                   for row in data["comparisons"]]
    return {"checkpoints": checkpoints[:6], "checkpoints_omitted": max(0, len(checkpoints) - 6),
            "comparisons": comparisons[:6], "comparisons_omitted": max(0, len(comparisons) - 6),
            "meaning": "checkpoint claims and gaps; comparisons are scope-checked, never latest-wins"}


def event_trace(args, store, providers, deadline, base_argv):
    """Trace one event/citation ref; its own recording is read first."""
    ref = cite.ref_argument(args.ref, getattr(args, "max_record_bytes", 2 * 1024 * 1024))
    if (not isinstance(ref.get("file"), str) or not ref["file"]
            or not isinstance(ref.get("sha256"), str) or not ref["sha256"]
            or not isinstance(ref.get("item", 0), int) or ref.get("item", 0) < 0):
        raise ValueError("Valid event refs need a file, sha256 and a nonnegative integer item")
    providers.recording_files = [ref["file"]]
    providers.load({"recordings", "operations", "checkpoints", "commit_links"})
    recording = next((item for item in providers.recordings
                      if item.get("file") == ref["file"]), None)
    if recording is None:
        raise ValueError("Referenced recording not found within scan bounds")
    target = _find(recording, ref)
    if target is None:
        raise ValueError("Referenced event not found within scan bounds")
    if target["ref"]["sha256"] != ref["sha256"]:
        raise ValueError("Source changed at this ref; search again")
    data = episode(recording, target, providers.tables.get("operations", []),
                   providers.tables.get("checkpoints", []), providers.tables.get("commit_links", []))
    coverage = {name: _compact({key: value for key, value in item.items() if key != "gaps"})
                for name, item in providers.coverage.items()}
    coverage.update(_compact(data["coverage"]))
    links, link_refs = _link_rows(providers, recording, data["request"])
    data["rows"] = data["rows"] + links
    coverage["episodes"] = _compact({k: v for k, v in providers.coverage.get("episodes", {}).items()})
    deepen, deepen_omitted = _deepen(base_argv, store, data)
    widen, widen_omitted = _widen(base_argv, recording, data, link_refs)
    deepen_items, widen_items = _unique(deepen), _unique(widen)
    partial = any(item.get("complete") is False for item in coverage.values())
    document = {"operation": "trace", "status": "partial" if partial else "ok",
                "anchor": {"kind": "event", "recording_id": recording.get("file"),
                           "event_id": target.get("event_id"), "ref": target.get("ref"),
                           "session": recording.get("session"), "harness": recording.get("harness")},
                "lanes": [{"lane": lane["lane"], "lane_role": lane["lane_role"],
                           "events": lane["events"], "first_order": lane["first_order"],
                           "last_order": lane["last_order"]} for lane in data["lanes"]],
                "dimensions": _package_dimensions(data["dimensions"],
                                                  "one anchor exchange; lane/branch-scoped evidence"),
                "reconciliation": _reconciliation_summary(data),
                "results": data["rows"], "coverage": coverage,
                "scope": {"repo": store.root, "retention": "transient", "frontier": data["frontier"],
                          "ordering": "physical order inside each branch lane; lanes are not merged",
                          "meaning": "evidence and claims, not a verified task verdict"},
                "next": deepen_items[:MAX_DEEPEN] + widen_items[:MAX_WIDEN],
                "next_omitted": {"deepen": max(deepen_omitted, len(deepen_items) - MAX_DEEPEN),
                                 "widen": max(widen_omitted, len(widen_items) - MAX_WIDEN)}}
    return document, digest([data["rows"], data["dimensions"], coverage])


def _link_rows(providers, recording, request):
    """(section "link" rows, widen refs) for the anchor request's episode (D141).

    Chronological where ordering is known; links with unknown ordering follow, flagged."""
    if request is None:
        return [], []
    providers.load({"episodes"})
    lookup = providers.episodes
    episode_id = episode_links.episode_for(lookup, recording.get("file"), request.get("ref"))
    if episode_id is None:
        return [], []
    rows, extra = [], []
    for link in providers.tables.get("episode_links", []):
        if episode_id not in (link["source"], link["target"]):
            continue
        later = link["source"] == episode_id
        other_id = link["target"] if later else link["source"]
        other = lookup["by_id"].get(other_id)
        ref = other.request["ref"] if other is not None else None
        if other_id == episode_id:
            ref = None
        rows.append({"section": "link", "episode_id": episode_id, "relation": link["relation"],
                     "strength": link["strength"], "basis": link["basis"], "matched": link["matched"],
                     "direction": "later" if later else "earlier", "target_episode": other_id,
                     "target": ref, "timestamp": other.request.get("timestamp") if other is not None else None,
                     "ordering_basis": link["ordering_basis"], "response": link["response"],
                     "alternatives": link["alternatives"], "evidence_refs": link["evidence_refs"],
                     "meaning": "candidate relation via shared features; not causal proof"})
    rows.sort(key=lambda r: (r["ordering_basis"] == "unknown", r["timestamp"] or ""))
    for row in rows:
        if row["ordering_basis"] == "unknown":
            row["ordering_unknown"] = True
        if isinstance(row["target"], dict):
            extra.append((row["target"], f"Trace the {row['relation'].replace('_', ' ')} episode"))
    return rows[:MAX_LINKS], extra


def _commit_fact(providers, sha):
    row = next((item for item in providers.tables.get("commits", []) if item.get("sha") == sha), None)
    if row:
        return {"sha": row.get("sha"), "subject": row.get("subject"),
                "committed_at": row.get("committed_at"), "author_name": row.get("author_name"),
                "basis": "git_log"}
    root = providers.resolver.repo(providers.store.root)
    if not root:
        return {"sha": sha, "subject": None, "committed_at": None, "author_name": None,
                "basis": "recorded_only", "git": "unavailable"}
    exists = providers.resolver.run(root, "cat-file", "-e", sha + "^{commit}")
    if exists is None:
        return None
    text = providers.resolver.run(root, "show", "-s", "--format=%H%x1f%s%x1f%cI%x1f%an", sha)
    parts = (text or "").strip().split("\x1f")
    return {"sha": parts[0] if parts and re.fullmatch(r"[0-9a-f]{40}", parts[0]) else sha,
            "subject": parts[1] if len(parts) > 1 else None,
            "committed_at": parts[2] if len(parts) > 2 else None,
            "author_name": parts[3] if len(parts) > 3 else None, "basis": "git_show"}


def _commit_changes(providers, sha):
    return [row for row in providers.tables.get("changes", []) if row.get("sha") == sha]


def _related(providers, commit_sha, changed_paths, witness_recordings):
    """Bounded noncausal context: recordings whose exact repository paths overlap the commit."""
    if not changed_paths:
        return [], 0
    rows, seen, omitted = [], set(), 0
    recordings = {item.get("file"): item for item in providers.recordings}
    for operation in providers.tables.get("operations", []):
        if operation.get("path_location") != "repository":
            continue
        recording_id = operation.get("recording_id")
        if recording_id in witness_recordings:
            continue
        path = providers.repo_path(operation)
        if path not in changed_paths:
            continue
        identity = (recording_id, operation.get("lane"))
        if identity in seen:
            continue
        seen.add(identity)
        if len(rows) >= MAX_RELATED:
            omitted += 1
            continue
        recording = recordings.get(recording_id)
        # The operation's own request, not the lane's first one.
        request = _find(recording, operation.get("request_ref"))
        if request is not None and request.get("request_origin") != "human":
            request = None
        rows.append({"section": "related", "relation": "related_context", "basis": "path_overlap",
                     "recording_id": recording_id, "lane": operation.get("lane"),
                     "path": path, "path_raw": operation.get("path_raw"),
                     "operation_id": operation.get("operation_id"), "action": operation.get("action"),
                     "ref": operation.get("call_ref") or operation.get("result_ref"),
                     "request_ref": request.get("ref") if request else None,
                     "request": message_segments.prose(request)[:300] if request else None,
                     "meaning": "shared path; not proof of authorship or contribution"})
    return rows, omitted


def commit_trace(args, store, providers, deadline, base_argv):
    """Trace one commit through every bounded witness lane plus labeled path-overlap context."""
    sha = args.ref.removeprefix("git:")
    providers.load({"commit_links", "operations", "checkpoints"})
    providers.load({"commits", "changes"})
    fact = _commit_fact(providers, sha)
    if fact is None:
        raise ValueError("Commit unavailable in the selected repository")
    witnesses_all = providers.tables.get("commit_links", [])
    witnesses = [row for row in witnesses_all
                 if row.get("sha") == sha or row.get("recorded_sha") == sha]
    archive_coverage, targeted_search = None, None
    if not witnesses:
        # The bounded archive is recency-ordered. An explicit commit must not be starved
        # by unrelated recent recordings: match its SHA/subject in candidate bytes, then
        # rescan only those recordings under the same event/byte budgets.
        matches, search = providers.match_recordings(sha, fact.get("subject"))
        targeted_search = search
        if matches:
            archive_coverage = {name: _compact({key: value for key, value in item.items()
                                               if key != "gaps"})
                                for name, item in providers.coverage.items()}
            providers.reload_recordings(matches)
            witnesses_all = providers.tables.get("commit_links", [])
            witnesses = [row for row in witnesses_all
                         if row.get("sha") == sha or row.get("recorded_sha") == sha]
    by_recording = {}
    for witness in witnesses:
        by_recording.setdefault(witness.get("recording_id"), []).append(witness)
    recordings = {item.get("file"): item for item in providers.recordings}
    lanes_data, rows, deepen, widen, comparisons, observations, claims = [], [], [], [], [], [], []
    deepen_omitted = widen_omitted = 0
    witness_recordings = set(by_recording)
    witness_count = 0
    for recording_id, found in by_recording.items():
        recording = recordings.get(recording_id)
        if recording is None:
            continue
        # Several commits can share one exchange; one episode owns all its rows, so a
        # second witness must not replay (and inflate) the same request/operation rows.
        exchanges = {}
        for witness in found:
            key = (json.dumps(witness.get("request_ref"), sort_keys=True)
                   if witness.get("request_ref") else witness.get("branch") or witness.get("event_id"))
            exchanges.setdefault(key, []).append(witness)
        for group in exchanges.values():
            target = _find(recording, group[0].get("ref"))
            if target is None:
                continue
            data = episode(recording, target, providers.tables.get("operations", []),
                           providers.tables.get("checkpoints", []), witnesses_all)
            witness_count += len(data["lanes"])
            for row in data["rows"]:
                row = dict(row)
                row.setdefault("lane_role", "commit_witness")
                rows.append(row)
            lanes_data.extend(data["lanes"])
            comparisons.extend(data["comparisons"])
            observations.extend(data["observations"])
            claims.extend(data["claims"])
            found_deepen, found_deepen_omitted = _deepen(base_argv, store, data)
            deepen.extend(found_deepen)
            deepen_omitted += found_deepen_omitted
            if data["request"] is not None:
                found_widen, found_widen_omitted = _widen(base_argv, recording, data)
                widen.extend(found_widen)
                widen_omitted += found_widen_omitted
    changed = _commit_changes(providers, sha)
    changed_paths = {row.get("path") for row in changed if row.get("path")}
    commit_in_window = any(row.get("sha") == sha for row in providers.tables.get("commits", []))
    related, omitted_related = _related(providers, sha, changed_paths, witness_recordings)
    rows += related
    related_widen = [_trace_ref(base_argv, row["ref"],
                                "Trace the exchange sharing a changed path (not proof of contribution)")
                     for row in related if isinstance(row.get("ref"), dict)]
    rows = [{"section": "commit_fact", "sha": fact.get("sha"), "subject": fact.get("subject"),
             "committed_at": fact.get("committed_at"), "author_name": fact.get("author_name"),
             "basis": fact.get("basis"), "changed_paths": sorted(changed_paths)[:12],
             "changed_paths_total": len(changed_paths)}] + rows
    witness_shas = sorted({row.get("sha") for row in witnesses if row.get("sha")})
    dimensions = reconciliation.summarize(observations, claims)
    # The commit itself is one Git fact; witnesses are separate recordings, not one task.
    dimensions["committed"] = {"evidence_presence": "observed", "outcome": "succeeded",
                               "latest": {"observation_id": f"commit:{fact.get('sha')}", "value": fact.get("sha"),
                                          "outcome": "succeeded"},
                               "observations": 1, "claims": 0, "values": [fact.get("sha")]}
    for dimension in reconciliation.DIMENSIONS:
        if dimension == "committed":
            continue
        if dimensions[dimension]["evidence_presence"] == "observed":
            dimensions[dimension]["scope"] = "witness lanes; separate exchanges"
    coverage = {name: _compact({key: value for key, value in item.items() if key != "gaps"})
                for name, item in providers.coverage.items()}
    operation_rows = [row for row in rows if row.get("section") == "operation"]
    unknown_operations = [row for row in operation_rows if row.get("outcome") not in ("succeeded", "failed")]
    unpaired_operations = [row for row in operation_rows if row.get("pairing") != "paired"]
    checkpoint_rows = [row for row in rows if row.get("section") == "checkpoint"]
    unresolved_checkpoints = [row for row in checkpoint_rows if row.get("boundary_status") != "resolved"]
    coverage["operations"] = {"complete": not unknown_operations and not unpaired_operations,
                              "used": len(operation_rows), "unknown_outcomes": len(unknown_operations),
                              "unpaired": len(unpaired_operations), "source": "witness-lane operations"}
    coverage["checkpoints"] = {"complete": not unresolved_checkpoints, "used": len(checkpoint_rows),
                               "unresolved_boundaries": len(unresolved_checkpoints),
                               "source": "witness-lane checkpoint boundaries"}
    coverage["links"] = {"complete": bool(witnesses), "used": len(witnesses),
                         "unresolved": 0 if witnesses else 1, "source": "commit witnesses"}
    coverage["episode"] = {"complete": not omitted_related and bool(witnesses)
                           and coverage["operations"]["complete"] and coverage["checkpoints"]["complete"],
                           "witness_lanes": witness_count, "witness_recordings": len(by_recording),
                           "related_returned": len(related), "related_omitted": omitted_related,
                           "witness_shas": witness_shas[:4],
                           "commit_changes": "available" if commit_in_window else "outside_git_window",
                           "none_found_in_scanned_scope": not witnesses}
    if not commit_in_window:
        # The commit exists, but its changed paths were not in the bounded Git window:
        # path-overlap context is then unsearched rather than empty.
        coverage["episode"]["complete"] = False
    if archive_coverage:
        archive_coverage["complete"] = all(item.get("complete", True)
                                           for item in archive_coverage.values())
        coverage["archive"] = archive_coverage
    if targeted_search is not None:
        targeted_search["complete"] = bool(targeted_search.get("complete", True))
        coverage["targeted_search"] = targeted_search
    coverage["reconciliation"] = {"complete": True, "comparisons": len(comparisons),
                                  "not_comparable": sum(item.get("relation") == "not_comparable"
                                                        for item in comparisons),
                                  "unsupported_claims": 0}
    if not witnesses:
        rows.append({"section": "gap", "area": "witness", "reason": "no_recording_witness_in_scanned_scope",
                     "sha": sha})
    partial = any(item.get("complete") is False for item in coverage.values())
    document = {"operation": "trace", "status": "partial" if partial else "ok",
                "anchor": {"kind": "commit", "sha": fact.get("sha"), "subject": fact.get("subject"),
                           "committed_at": fact.get("committed_at"), "basis": fact.get("basis")},
                "lanes": [{"lane": lane["lane"], "events": lane["events"],
                           "first_order": lane["first_order"], "last_order": lane["last_order"],
                           "lane_role": "commit_witness"} for lane in lanes_data],
                "dimensions": _package_dimensions(dimensions,
                                                  "commit fact plus separate witness lanes; related context is not contribution proof"),
                "reconciliation": {"checkpoints": [], "checkpoints_omitted": 0,
                                   "comparisons": [{key: row.get(key) for key in
                                                    ("relation", "reason", "dimension", "checkpoint_id",
                                                     "claim", "observation_value", "observation_order",
                                                     "boundary_order")} for row in comparisons[:6]],
                                   "comparisons_omitted": max(0, len(comparisons) - 6),
                                   "meaning": "witness-lane claims and gaps; related context is not a task verdict"},
                "results": rows, "coverage": coverage,
                "scope": {"repo": store.root, "retention": "transient",
                          "ordering": "witness lanes stay separate; path-overlap context is labeled",
                          "meaning": "witness presence proves a recorded commit result, not authorship of every change"},
                "next": _unique(deepen)[:MAX_DEEPEN] + _unique(related_widen + widen)[:MAX_WIDEN],
                "next_omitted": {
                    "deepen": max(deepen_omitted, len(_unique(deepen)) - MAX_DEEPEN),
                    "widen": max(widen_omitted, len(_unique(related_widen + widen)) - MAX_WIDEN)}}
    return document, digest([rows, dimensions, coverage])


def _unique(items):
    found, seen = [], set()
    for item in items:
        ref = item.get("ref") if isinstance(item, dict) else None
        if ref in seen:
            continue
        seen.add(ref)
        found.append(item)
    return found


def build(args, store, providers, deadline, base_argv):
    """(document, signature) for one trace invocation."""
    if isinstance(args.ref, str) and SHA.fullmatch(args.ref.strip()):
        args.ref = args.ref.strip()
        return commit_trace(args, store, providers, deadline, base_argv)
    return event_trace(args, store, providers, deadline, base_argv)
