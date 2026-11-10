"""Invocation-local operation and checkpoint facts derived from already-scanned events.

An operation is one recorded tool call plus its exactly paired result. Path
associations form separate rows under one stable operation ID; a pathless
operation keeps one null-path row. Outcomes come from the recorded result only:
a missing, malformed or failed result never becomes a successful edit.

Checkpoints preserve the published claim and resolve its coverage boundary by
recorded branch identity, never by publication time.
"""
import json

from .profiles import pointer, resolve_path
from .reader import message_graph

OPERATION_GRAIN = "one operation/path association; counts use distinct operation_id"
CHECKPOINT_GRAIN = "one original checkpoint item; join events USING(recording_id)"
OPERATION_COLUMNS = {
    "operation_id": "VARCHAR", "association_key": "VARCHAR", "recording_id": "VARCHAR",
    "session": "VARCHAR", "independent_session": "VARCHAR", "harness": "VARCHAR",
    "lane": "VARCHAR", "order": "BIGINT", "association": "BIGINT", "timestamp": "VARCHAR",
    "tool": "VARCHAR", "action": "VARCHAR", "pairing": "VARCHAR",
    "call_event_id": "VARCHAR", "result_event_id": "VARCHAR", "call_ref": "JSON", "result_ref": "JSON",
    "turn": "VARCHAR", "request_ref": "JSON", "request_basis": "VARCHAR", "request_origin": "VARCHAR",
    "path": "VARCHAR", "path_raw": "VARCHAR", "path_location": "VARCHAR",
    "command": "VARCHAR", "outcome": "VARCHAR", "exit_code": "BIGINT",
    "interpretation": "VARCHAR", "adapter": "VARCHAR", "basis": "VARCHAR",
    "content_ref": "JSON", "content_chars": "BIGINT", "text_hash": "VARCHAR",
}
CHECKPOINT_COLUMNS = {
    "checkpoint_id": "VARCHAR", "recording_id": "VARCHAR", "session": "VARCHAR",
    "independent_session": "VARCHAR", "harness": "VARCHAR", "lane": "VARCHAR", "branch": "VARCHAR",
    "order": "BIGINT", "timestamp": "VARCHAR", "publication_event_id": "VARCHAR",
    "publication_ref": "JSON", "original_event_id": "VARCHAR", "coverage_end_id": "VARCHAR",
    "boundary_status": "VARCHAR", "boundary_ref": "JSON", "boundary_event_id": "VARCHAR",
    "boundary_order": "BIGINT", "boundary_items": "BIGINT", "structured": "JSON", "version": "VARCHAR",
    "checkpoint_key": "VARCHAR", "coverage_start_id": "VARCHAR", "prepared_leaf_id": "VARCHAR",
    "published_leaf_id": "VARCHAR", "retained_anchor": "VARCHAR", "read_files": "JSON",
    "modified_files": "JSON", "adapter": "VARCHAR", "basis": "VARCHAR", "interpretation": "VARCHAR",
}
STRUCTURED_GIT = {"git_plan_context": "plan_snapshot", "git_commit_plan": "commit_plan",
                  "git_record": "commit_record"}
KNOWN_ACTIONS = {"file_read", "file_edit", "command"}


def _decoded(event, cache):
    """The original decoded payload for one event, or the event text when it is unavailable."""
    try:
        if event.get("kind") == "tool_result":
            return cache.payload(event)
        return pointer(cache.get(event["ref"]), event.get("payload_pointer", ""))
    except (KeyError, IndexError, TypeError, ValueError, OSError):
        text = event.get("text")
        if isinstance(text, str):
            try:
                return json.loads(text)
            except ValueError:
                return text
        return None


def _object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            found = json.loads(value)
            return found if isinstance(found, dict) else None
        except ValueError:
            return None
    return None


def _associations(event, decoded, recording):
    """[(path, raw, location, basis)] for one operation; [] becomes one null-path row."""
    tool = event.get("tool")
    if tool == "git_plan_context":
        payload = _object(decoded) or {}
        root = payload.get("repositoryRoot") or event.get("cwd") or recording.get("root")
        rows = []
        for change in payload.get("changes") or []:
            raw = change.get("path") if isinstance(change, dict) else None
            if not isinstance(raw, str) or not raw:
                continue
            path, location = resolve_path(raw, root, root)
            rows.append((path, raw, location, "planning_snapshot"))
        return rows
    if tool == "git_commit_plan":
        return []
    path = event.get("path")
    if not path:
        return []
    return [(path, event.get("path_raw") or path, event.get("path_location") or "unresolved", "recorded_tool")]


def _operation_id(event):
    recording = event.get("recording_id")
    if event.get("kind") == "tool_result":
        return f"{recording}#result:{event.get('event_id')}"
    if event.get("kind") == "file_edit":
        return f"{recording}#change:{event.get('ref', {}).get('offset')}"
    return f"{recording}#call:{event.get('event_id')}"


def _edit_or_call(event):
    if event.get("kind") == "tool_call":
        return True
    return event.get("kind") in ("command", "file_read", "file_edit")


def operations(recordings, cache):
    """(rows, coverage) for every scanned operation, before delivery filters."""
    rows = []
    coverage = {"complete": True, "operations": 0, "rows": 0, "paired": 0, "call_without_result": 0,
                "result_without_call": 0, "unknown_outcome": 0, "failed": 0, "unsupported_tools": {},
                "interpretation": {"native": 0, "structured": 0, "generic": 0, "malformed": 0,
                                   "missing_result": 0},
                "locations": {}, "plan_snapshots": 0, "commit_plans": 0, "commit_records": 0}
    for recording in recordings:
        by_ref = {(e["ref"]["offset"], e["ref"].get("item", 0)): e for e in recording["events"]}
        seen_operations = set()
        association_counts = {}
        for event in recording["events"]:
            if event.get("kind") == "tool_result":
                if event.get("call_ref"):
                    continue
                pairing, call_ref, result_ref = "result_without_call", None, event.get("ref")
            elif _edit_or_call(event):
                pairing = "paired" if event.get("result_ref") else "call_without_result"
                call_ref, result_ref = event.get("ref"), event.get("result_ref")
            else:
                continue
            operation_id = _operation_id(event)
            new_operation = operation_id not in seen_operations
            # Outcome belongs to every association of this operation, not only its first row.
            outcome = event.get("outcome") or "unknown"
            if new_operation:
                seen_operations.add(operation_id)
                coverage["operations"] += 1
                coverage[pairing] += 1
                if outcome == "unknown":
                    coverage["unknown_outcome"] += 1
                elif outcome == "failed":
                    coverage["failed"] += 1
            tool = event.get("tool") or event.get("kind")
            tool_name = event.get("tool")
            action = STRUCTURED_GIT.get(tool_name) or event.get("operation") or event.get("kind") or "unknown"
            if tool_name == "git_plan_context":
                if new_operation: coverage["plan_snapshots"] += 1
                interpretation, adapter, basis = ("structured" if event.get("result_ref") else "missing_result",
                                                  "pi:git_plan_context", "planning_snapshot")
            elif tool_name == "git_commit_plan":
                if new_operation: coverage["commit_plans"] += 1
                interpretation, adapter, basis = ("structured" if event.get("result_ref") else "missing_result",
                                                  "pi:git_commit_plan", "structured_commit")
            elif tool_name == "git_record":
                if new_operation: coverage["commit_records"] += 1
                interpretation, adapter, basis = ("structured" if event.get("result_ref") else "missing_result",
                                                  "pi:git_record", "structured_record")
            elif action in KNOWN_ACTIONS:
                interpretation, adapter, basis = "native", f"{event.get('harness')}:{tool_name or action}", "recorded_tool"
            else:
                interpretation, adapter, basis = "generic", f"{event.get('harness')}:{tool_name or event.get('kind')}", "recorded_tool"
                if new_operation:
                    coverage["unsupported_tools"][tool] = coverage["unsupported_tools"].get(tool, 0) + 1
            source_event = event
            if tool_name in STRUCTURED_GIT and event.get("result_ref"):
                source_event = by_ref.get((event["result_ref"].get("offset"),
                                           event["result_ref"].get("item", 0)), event)
            decoded = _decoded(source_event, cache)
            associations = _associations(event, decoded, recording)
            if tool_name in STRUCTURED_GIT and not associations and tool_name == "git_plan_context":
                interpretation = "malformed" if not _object(decoded) else interpretation
            if new_operation:
                coverage["interpretation"][interpretation] = coverage["interpretation"].get(interpretation, 0) + 1
            result_event_id = None
            if pairing == "result_without_call":
                result_event_id = event.get("event_id")
            elif event.get("result_ref"):
                paired = by_ref.get((event["result_ref"].get("offset", -1),
                                     event["result_ref"].get("item", 0)))
                result_event_id = paired.get("event_id") if paired else None
            # Association indexes run over the whole operation, so multi-item tools
            # cannot reuse a key that another path of the same call already holds.
            for association in (associations or [(None, None, None, None)]):
                index = association_counts.get(operation_id, 0)
                association_counts[operation_id] = index + 1
                path, raw, location, path_basis = association
                coverage["locations"][location or "none"] = coverage["locations"].get(location or "none", 0) + 1
                rows.append({
                    "operation_id": operation_id,
                    "association_key": f"{operation_id}:{index}",
                    "recording_id": event.get("recording_id"), "session": recording.get("session"),
                    "independent_session": event.get("independent_session"), "harness": event.get("harness"),
                    "lane": event.get("lane"), "order": event.get("order"), "association": index,
                    "timestamp": event.get("timestamp"), "tool": tool, "action": action, "pairing": pairing,
                    "call_event_id": event.get("event_id") if call_ref else None,
                    "result_event_id": result_event_id,
                    "call_ref": call_ref, "result_ref": result_ref,
                    "turn": event.get("turn"), "request_ref": event.get("request_ref"),
                    "request_basis": event.get("request_basis"), "request_origin": event.get("request_origin"),
                    "path": path, "path_raw": raw, "path_location": location,
                    "command": event.get("command"), "outcome": outcome, "exit_code": event.get("exit_code"),
                    "interpretation": interpretation, "adapter": adapter,
                    "basis": path_basis or basis, "content_ref": result_ref or call_ref,
                    "content_chars": event.get("text_chars"), "text_hash": event.get("text_hash"),
                })
    coverage["rows"] = len(rows)
    coverage["complete"] = not (coverage["call_without_result"] or coverage["result_without_call"]
                                or coverage["interpretation"]["missing_result"]
                                or coverage["interpretation"]["malformed"])
    return rows, coverage


def checkpoints(recordings, cache):
    """(rows, coverage) for original checkpoints and their branch-resolved coverage boundaries."""
    rows = []
    coverage = {"complete": True, "checkpoints": 0, "resolved": 0, "missing": 0, "ambiguous": 0,
                "off_branch": 0, "invalid": 0, "unsupported": 0}
    for recording in recordings:
        events = recording["events"]
        graph = message_graph(events)
        for event in events:
            if event.get("kind") != "checkpoint":
                continue
            coverage["checkpoints"] += 1
            payload = _decoded(event, cache)
            structured = payload if isinstance(payload, (dict, list)) else event.get("text")
            coverage_end = event.get("coverage_end_id")
            own_message = event.get("external_message_id")
            origin, origin_status = graph["chain"](own_message) if own_message else ([], "missing")
            boundary = None
            if not isinstance(coverage_end, str) or not coverage_end:
                status = "invalid"
            elif coverage_end == own_message:
                # A summary cannot end at the message that publishes it.
                status = "invalid"
            elif coverage_end not in graph["by_id"]:
                status = "missing"
            elif graph["ambiguous"](coverage_end):
                status = "ambiguous"
            elif origin_status in ("missing", "cycle", "ambiguous"):
                # Without supported ancestry, absence from the chain is not proof of another branch.
                status = "ambiguous"
            elif coverage_end in origin:
                boundary, status = graph["end_event"](coverage_end), "resolved"
            else:
                # Keep the recorded candidate visible, but never treat it as a usable boundary.
                boundary, status = graph["end_event"](coverage_end), "off_branch"
            coverage[status] += 1
            if not isinstance(structured, (dict, list)):
                coverage["unsupported"] += 1
            # The Pi v3 envelope owns the schema version; older flat checkpoints keep it in `structured`.
            version = event.get("schema_version")
            if version is None and isinstance(structured, dict):
                version = structured.get("version")
            boundary_group = graph["by_id"].get(coverage_end) or []
            rows.append({
                "checkpoint_id": f"{event.get('recording_id')}#{event.get('event_id')}",
                "recording_id": event.get("recording_id"), "session": recording.get("session"),
                "independent_session": event.get("independent_session"), "harness": event.get("harness"),
                "lane": event.get("lane"), "branch": event.get("branch"),
                "order": event.get("order"), "timestamp": event.get("timestamp"),
                "publication_event_id": event.get("event_id"), "publication_ref": event.get("ref"),
                "original_event_id": event.get("event_id"), "coverage_end_id": coverage_end,
                "boundary_status": status, "boundary_ref": boundary.get("ref") if boundary else None,
                "boundary_event_id": boundary.get("event_id") if boundary else None,
                "boundary_order": boundary.get("order") if boundary else None,
                "boundary_items": len(boundary_group) if boundary_group else None,
                "structured": structured, "version": str(version) if version is not None else None,
                "checkpoint_key": event.get("checkpoint_id"),
                "coverage_start_id": event.get("coverage_start_id"),
                "prepared_leaf_id": event.get("prepared_at_leaf"),
                "published_leaf_id": event.get("published_at_leaf"),
                "retained_anchor": event.get("retained_anchor"),
                "read_files": event.get("read_files"), "modified_files": event.get("modified_files"),
                "adapter": f"{event.get('harness')}:checkpoint", "basis": "recorded_checkpoint",
                "interpretation": "structured" if isinstance(structured, (dict, list)) else "unsupported",
            })
    coverage["complete"] = not (coverage["missing"] or coverage["ambiguous"] or coverage["invalid"]
                                or coverage["unsupported"] or coverage["off_branch"])
    return rows, coverage
