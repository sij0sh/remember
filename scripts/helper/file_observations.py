"""File observations and changes reconstructed from recorded tool results (D142-D147).

An observation is what one successful operation shows of one repository file:
``read_result`` (a read adapter's text), ``write_payload`` (whole written content),
``reconstructed`` (a complete base replayed exactly through the recorded patch),
``native_patch`` (hunks only) or ``planning_diff`` (a Git planning snapshot's diff).
``content_sha256`` hashes decoded UTF-8 text; ``complete`` needs positive evidence and
``unknown`` carries no hash.

A change is ``direct`` (one recorded native edit), ``associated`` (shell commands or
several edits lie between the observations) or ``unknown``. Candidate operations are
leads, never attribution; a path missing from a later snapshot is never ``deleted``.
"""
import hashlib

from . import edit_hunks
from .operation_facts import _decoded, _object
from .profiles import pointer, read_view

OBSERVATION_GRAIN = "one observation of one repository file by one operation"
CHANGE_GRAIN = "one recorded or inferred change of one repository file"
OBSERVATION_COLUMNS = {
    "observation_id": "VARCHAR", "operation_id": "VARCHAR", "recording_id": "VARCHAR", "session": "VARCHAR",
    "harness": "VARCHAR", "lane": "VARCHAR", "order": "BIGINT", "timestamp": "VARCHAR", "tool": "VARCHAR",
    "path": "VARCHAR", "kind": "VARCHAR", "completeness": "VARCHAR", "start_line": "BIGINT",
    "line_count": "BIGINT", "total_lines": "BIGINT", "request": "JSON", "content_sha256": "VARCHAR",
    "hash_basis": "VARCHAR", "adapter": "VARCHAR", "ref": "JSON"}
CHANGE_COLUMNS = {
    "change_id": "VARCHAR", "recording_id": "VARCHAR", "session": "VARCHAR", "lane": "VARCHAR",
    "order": "BIGINT", "timestamp": "VARCHAR", "path": "VARCHAR", "basis": "VARCHAR",
    "attribution": "VARCHAR", "change_state": "VARCHAR", "uncertainty_reason": "VARCHAR",
    "operation_id": "VARCHAR", "candidate_ops": "JSON", "before_observation": "VARCHAR",
    "after_observation": "VARCHAR", "ref": "JSON"}
EDIT_KINDS = ("write_payload", "reconstructed", "native_patch")
MAX_CANDIDATES = 5


def sha(text):
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest() if isinstance(text, str) else None


def _record(cache, event):
    try:
        return cache.get(event["ref"]) if event else None
    except (OSError, KeyError, ValueError):
        return None


def _arguments(cache, event):
    record = _record(cache, event)
    try:
        found = pointer(record, event.get("payload_pointer")) if record and event.get("payload_pointer") else None
    except (KeyError, IndexError, TypeError, ValueError):
        found = None
    # A harness may record arguments as a JSON string (dsh tool-call parts).
    return found if isinstance(found, dict) else _object(found) or {}


def _base(op, path, kind, number=0):
    return {"observation_id": f"{op['association_key']}:{number}", "operation_id": op["operation_id"],
            "recording_id": op.get("recording_id"), "session": op.get("session"), "harness": op.get("harness"),
            "lane": op.get("lane"), "order": op.get("order"), "timestamp": op.get("timestamp"),
            "tool": op.get("tool"), "path": path, "kind": kind, "completeness": "unknown", "start_line": None,
            "line_count": None, "total_lines": None, "request": None, "content_sha256": None,
            "hash_basis": None, "adapter": None, "ref": op.get("result_ref") or op.get("call_ref")}


def _content(row, content, **fields):
    """Fill an observation's extent and hash; ``unknown`` keeps no hash (D143)."""
    row.update(fields)
    if row["completeness"] != "unknown" and isinstance(content, str):
        row.update(content_sha256=sha(content), hash_basis="utf8_text", _content=content)
    return row


def _read(op, path, call, result, cache):
    arguments = _arguments(cache, call)
    try:
        text = cache.payload(result) if result else None
    except (OSError, KeyError, ValueError):
        text = None
    view = read_view(op.get("harness"), _record(cache, result), text, arguments)
    row = _base(op, path, "read_result")
    row["request"] = {k: arguments.get(k) for k in ("offset", "limit") if arguments.get(k) is not None}
    if not view:
        return {**row, "adapter": f"{op.get('harness')}:unrecognized"}
    return _content(row, view["content"], **{k: view[k] for k in (
        "start_line", "line_count", "total_lines", "completeness", "adapter")})


def _lines(text):
    return text.count("\n") + bool(text and not text.endswith("\n"))


def _edit(op, path, events, cache, base):
    """One edit observation; a patch over a complete base that replays exactly is ``reconstructed``."""
    for event in events:
        extract = edit_hunks._extractor(event) if event else None
        if not extract:
            continue
        try:
            found, _ = extract(event, cache.get(event["ref"]))
        except (OSError, ValueError, KeyError):
            found = None
        if not found:
            continue
        change, source, hunks, original, after, truncated = found
        whole = op.get("tool") in ("Write", "write") or change in ("add", "write")
        if not truncated and not whole and not isinstance(original, str) and isinstance(base, str):
            # Recorded pre-edit file (Claude originalFile) or an earlier complete observation.
            after = edit_hunks.replay(base, hunks)
        if isinstance(after, str) and not truncated:
            return _content(_base(op, path, "write_payload" if whole else "reconstructed"), after,
                            completeness="complete", adapter=source, start_line=1, line_count=_lines(after))
        return {**_base(op, path, "native_patch"), "completeness": "truncated" if truncated else "patch",
                "adapter": source}
    return None


def _plans(op, rows, result, cache):
    """(observations, snapshot) for one planning snapshot; the snapshot lists every changed path."""
    payload = _object(_decoded(result, cache)) if result else None
    changes = {c.get("path"): c for c in (payload or {}).get("changes") or [] if isinstance(c, dict)}
    out, paths = [], {}
    for number, (association, path) in enumerate(rows):
        change = changes.get(association.get("path_raw"))
        if not change:
            continue
        row = _base(association, path, "planning_diff", number)
        row["_shape"] = (change.get("indexStatus"), change.get("worktreeStatus"))
        row["_odd"] = "binary" if change.get("binary") else "rename" if change.get("status") in (
            "renamed", "copied") else None
        if isinstance(change.get("diff"), str):
            _content(row, change["diff"], completeness="patch", adapter="pi:git_plan_context")
        else:
            row["adapter"] = "pi:git_plan_context"
        out.append(row)
        paths[path] = row
    snapshot = {"recording_id": op.get("recording_id"), "lane": op.get("lane"), "order": op.get("order") or 0,
                "base": (payload or {}).get("base"), "paths": paths}
    return out, snapshot


def read_pairs(observations):
    """[(earlier, later, state)] for consecutive reads of one path on one recorded lane.

    ``edited_between`` (an edit observation lies between), ``incomparable`` (not both
    complete with hashes), else ``unchanged`` or ``changed`` by content hash (D147).
    """
    groups = {}
    for row in observations:
        groups.setdefault((row["recording_id"], row.get("lane"), row["path"]), []).append(row)
    out = []
    for key in sorted(groups, key=lambda k: tuple(str(x) for x in k)):
        last, edited = None, False
        for row in sorted(groups[key], key=lambda r: (r.get("order") or 0, r["observation_id"])):
            if row["kind"] in EDIT_KINDS:
                edited = True
            elif row["kind"] == "read_result":
                if last is not None:
                    out.append((last, row, _state(last, row, edited)))
                last, edited = row, False
    return out


def _state(a, b, edited):
    if edited:
        return "edited_between"
    if not (a["completeness"] == b["completeness"] == "complete" and a["content_sha256"] and b["content_sha256"]):
        return "incomparable"
    return "unchanged" if a["content_sha256"] == b["content_sha256"] else "changed"


def build(recordings, cache, operations, repo_path):
    """(observations, changes, coverage) over successful repository-path operations."""
    events = {(e.get("recording_id"), e.get("event_id")): e for r in recordings for e in r["events"]}
    coverage = {"complete": True, "observations": 0, "kinds": {}, "completeness": {}, "unrecognized_reads": 0,
                "edits_without_observation": 0, "changes": {}, "attribution": {}, "hash_basis": "utf8_text",
                "meaning": "recorded views of files; candidates are leads, not attribution"}
    by_op = {}
    for op in operations:
        path = repo_path(op) if op.get("path_location") == "repository" else None
        if path and op.get("outcome") == "succeeded":
            by_op.setdefault(op["operation_id"], []).append((op, path))
    observations, snapshots, latest = [], [], {}
    for operation_id in sorted(by_op, key=lambda o: (by_op[o][0][0].get("recording_id") or "",
                                                     by_op[o][0][0].get("order") or 0, o)):
        rows = by_op[operation_id]
        op, path = rows[0]
        key = (op.get("recording_id"), op.get("lane"), path)
        call = events.get((op.get("recording_id"), op.get("call_event_id")))
        result = events.get((op.get("recording_id"), op.get("result_event_id")))
        action = op.get("action")
        if action == "file_read":
            found = [_read(op, path, call, result, cache)]
            coverage["unrecognized_reads"] += found[0]["adapter"].endswith(":unrecognized")
        elif action == "file_edit":
            found = [_edit(op, path, (result, call), cache, latest.get(key))]
            if not found[0]:
                coverage["edits_without_observation"] += 1
                found = []
            latest.pop(key, None)  # a later replay needs this edit's own complete result
        elif action == "plan_snapshot":
            found, snapshot = _plans(op, rows, result, cache)
            snapshots.append(snapshot)
        else:
            continue
        for row in found:
            if row["completeness"] == "complete" and row.get("_content") is not None:
                latest[(row["recording_id"], row.get("lane"), row["path"])] = row["_content"]
        observations += found
    changes = _changes(observations, snapshots, operations)
    for row in observations:
        coverage["kinds"][row["kind"]] = coverage["kinds"].get(row["kind"], 0) + 1
        coverage["completeness"][row["completeness"]] = coverage["completeness"].get(row["completeness"], 0) + 1
        for name in [k for k in row if k.startswith("_")]:
            del row[name]
    coverage["observations"] = len(observations)
    for row in changes:
        coverage["changes"][row["basis"]] = coverage["changes"].get(row["basis"], 0) + 1
        coverage["attribution"][row["attribution"]] = coverage["attribution"].get(row["attribution"], 0) + 1
    return observations, changes, coverage


def _change(row, basis, attribution, candidates, before=None, operation=None, state="changed", reason=None):
    return {"change_id": f"{basis}:{row['observation_id']}", "recording_id": row["recording_id"],
            "session": row.get("session"), "lane": row.get("lane"), "order": row.get("order"),
            "timestamp": row.get("timestamp"), "path": row["path"], "basis": basis, "attribution": attribution,
            "change_state": state, "uncertainty_reason": reason, "operation_id": operation,
            "candidate_ops": candidates[-MAX_CANDIDATES:], "before_observation": before,
            "after_observation": row["observation_id"], "ref": row["ref"]}


def _changes(observations, snapshots, operations):
    lanes = {}
    for op in operations:
        lanes.setdefault((op.get("recording_id"), op.get("lane")), {})[op["operation_id"]] = op
    def between(row, low, high, path=None):
        """(native edit ops on path, command ops) strictly between two orders on the row's lane."""
        found = sorted(lanes.get((row["recording_id"], row.get("lane")), {}).values(),
                       key=lambda o: (o.get("order") or 0, o["operation_id"]))
        found = [o for o in found if low < (o.get("order") or 0) < high and o.get("outcome") != "failed"]
        edits = [o["operation_id"] for o in found if o.get("action") == "file_edit" and o.get("path") == path]
        return list(dict.fromkeys(edits)), list(dict.fromkeys(o["operation_id"] for o in found
                                                              if o.get("action") == "command"))
    def attribute(edits, commands):
        if commands:
            return "associated", None
        if len(edits) == 1:
            return "direct", edits[0]
        return ("associated" if edits else "unknown"), None
    changes = [_change(row, "direct_edit", "direct", [row["operation_id"]], operation=row["operation_id"])
               for row in observations if row["kind"] in EDIT_KINDS]
    by_lane = {}
    for snapshot in snapshots:
        by_lane.setdefault((snapshot["recording_id"], snapshot["lane"]), []).append(snapshot)
    for group in by_lane.values():
        group.sort(key=lambda s: s["order"])
        for earlier, later in zip(group, group[1:]):
            for path in sorted(set(earlier["paths"]) | set(later["paths"])):
                before, after = earlier["paths"].get(path), later["paths"].get(path)
                if before and after and before["content_sha256"] and before["content_sha256"] == after[
                        "content_sha256"]:
                    continue
                anchor = after or before
                edits, commands = between(anchor, earlier["order"], later["order"], path)
                attribution, operation = attribute(edits, commands)
                reason = ("head_changed" if earlier["base"] != later["base"] else
                          "missing_from_later_snapshot" if not after else
                          (after.get("_odd") or (before or {}).get("_odd")) or
                          ("missing_baseline" if before and not before["content_sha256"] else None) or
                          ("diff_kind_changed" if before and before["_shape"] != after["_shape"] else None))
                row = _change(anchor, "planning_diff", attribution if not reason else "unknown",
                              edits + commands, before["observation_id"] if before else None,
                              operation if not reason else None, "unknown" if reason else "changed", reason)
                if not after:
                    row["change_id"] = f"planning_diff:{later['order']}:{before['observation_id']}"
                    row["after_observation"] = None
                changes.append(row)
    for before, after, state in read_pairs(observations):
        if state == "changed":
            edits, commands = between(after, before.get("order") or 0, after.get("order") or 0, after["path"])
            changes.append(_change(after, "changed_between_reads", "associated" if commands else "unknown",
                                   commands, before["observation_id"]))
    changes.sort(key=lambda c: (c["recording_id"] or "", c.get("lane") or "", c.get("order") or 0, c["change_id"]))
    return changes


def target_rows(providers, path, selected, qualname=None):
    """(observation rows, change rows) for one target, tagged by section; symbols keep selected operations."""
    operations = {row.get("operation_id") for row in selected}
    def keep(row):
        return row.get("path") == path and (not qualname or row.get("operation_id") in operations)
    observed = [{"section": "file_observation", **row} for row in providers.tables.get("file_observations", [])
                if keep(row)]
    changed = [{"section": "change", **row} for row in providers.tables.get("change_observations", [])
               if keep(row)]
    return observed, changed
