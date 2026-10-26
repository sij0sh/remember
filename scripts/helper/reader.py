"""Bounded JSONL scan: profiles produce events; bytes stay in the source."""
import hashlib
import json
import re
from pathlib import Path

from .profiles import (PROFILES, content_text, detected, field, first, interpret,
                       matches, pointer, recorded_root, resolve_path)
from .records import records, safe_ref  # re-exported for existing callers


def _match_at(text, needle):
    if not text or not needle:
        return None
    try:
        found = re.search(re.escape(needle), str(text), re.IGNORECASE)
        return found.start() if found else None
    except re.error:
        return None


def _native(record, key):
    if not isinstance(record, dict):
        return "unknown" if key == "type" else None
    payload = record.get("payload")
    return record.get(key) or (payload.get(key) if isinstance(payload, dict) else None) or ("unknown" if key == "type" else None)


def _plain(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        text = content_text(value)
        if text:
            return text
        return "\n".join(str(x.get("text")) if isinstance(x, dict) and isinstance(x.get("text"), str)
                          else _plain(x) for x in value if x is not None)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=True)
    return "" if value is None else str(value)


def _store_text(event, text, options, scope):
    """Store full text when the per-group budget allows; otherwise a centered preview."""
    full = _plain(text)
    needle = next((n for n in options.text if n.casefold() in full.casefold()), None)
    event["match_at"] = _match_at(full, needle)
    event["text_hash"] = hashlib.sha256(full.encode("utf-8")).hexdigest() if full else None
    if not full:
        event.update(text="", text_chars=0, text_start=0, text_omitted=0, text_state="absent")
        return
    size = len(full.encode("utf-8"))
    if scope["text_bytes_stored"] + size <= options.max_text_bytes:
        scope["text_bytes_stored"] += size
        event.update(text=full, text_chars=len(full), text_start=0, text_omitted=0, text_state="full")
        return
    scope["text_truncated_events"] += 1
    scope["text_bytes_omitted"] += size
    scope["text_complete"] = False
    from .envelope import clip
    shown = clip(full, options.preview_chars, needle)
    event.update(shown, text_state="budget_omitted")


def scan_file(path, options, scope, ranges=None):
    profile = None
    events = []
    root = None
    external_sid = version = parent_session = None
    sid = str(Path(path).absolute())
    if ranges:
        # A range scan deliberately skips bytes; do not call it complete.
        scope["scan_complete"] = False
        scope["range_scan"] = True
    if options.harness == "auto":
        from .discovery import header
        harness, hinted_root, _ = header(path, options.max_record_bytes, min(262144, options.max_scan_bytes))
        if harness:
            profile, root = PROFILES[harness], hinted_root
    # Only small projections survive a record; payloads remain in the source.
    for ref, record, error in records(path, scope, options.max_record_bytes, options.max_scan_bytes, ranges):
        if scope["events_projected"] >= options.max_events:
            scope["scan_complete"] = False
            scope["event_limit_reached"] = True
            break
        if record is not None:
            harness = options.harness if options.harness != "auto" else detected(record)
            if profile is None and harness:
                profile = PROFILES[harness]
            root = root or recorded_root(record, profile and profile["harness"])
        local = 0
        def add(kind, text="", payload_pointer=None, **fields):
            nonlocal local
            if scope["events_projected"] >= options.max_events:
                scope["scan_complete"] = False
                scope["event_limit_reached"] = True
                return
            event_ref = dict(ref, item=local, pointer=fields.pop("pointer", ""))
            local += 1
            state = fields.pop("state", "present")
            text_state = fields.pop("text_state", None)
            origin = fields.pop("origin", None)
            channel = fields.pop("channel", "recorded")
            purpose = fields.pop("purpose", None)
            role = fields.pop("role", None)
            if role is None:
                role = "user" if kind == "user_message" else "assistant" if kind in (
                    "assistant_message", "summary", "reasoning") else "system" if kind in (
                    "instruction", "config", "lifecycle", "capability", "usage", "context_change") else "tool"
            if origin is None:
                origin = "human" if kind == "user_message" else "assistant" if kind == "assistant_message" else (
                    "tool" if kind in ("tool_call", "tool_result", "command", "file_read", "file_edit") else
                    "system" if kind == "instruction" else "harness")
            e = {"kind": kind, "role": role, "origin": origin, "channel": channel,
                 "harness": profile["harness"] if profile else "unknown", "session": sid,
                 "state": state, "event_id": f"{Path(path).name}:{ref['line'] if ref['line'] is not None else ref['offset']}:{local}",
                 "recording_id": str(Path(path).absolute()),
                 "native_type": _native(record, "type"),
                 "native_subtype": _native(record, "subtype"),
                 "profile_version": profile.get("version") if profile else None,
                 "ref": event_ref, "order": ref["line"] if ref["line"] is not None else ref["offset"], **fields}
            if kind == "command" or (kind == "tool_call" and fields.get("operation") == "command"):
                # One row per shell invocation in every harness: a codex command, a pi/claude-code shell tool call.
                e["is_shell"] = True
            if purpose is not None:
                e["purpose"] = purpose
            if payload_pointer is not None:
                e["payload_pointer"] = payload_pointer
            if record and profile:
                for key, specs in profile.get("correlation", {}).items():
                    e[key] = first(specs, record)
                e["timestamp"] = first(profile.get("timestamp", []), record)
            needles = options.text or ()
            searchable = str(text) + "\n" + "\n".join(str(fields.get(k, "")) for k in ("path", "command", "tool"))
            e["_matched"] = any(n.casefold() in searchable.casefold() for n in needles)
            _store_text(e, text, options, scope)
            if text_state:
                e["text_state"] = text_state
            for name, value in list(e.items()):
                if name not in ("session", "ref", "text") and isinstance(value, str) and len(value) > 2048:
                    e[name] = value[:2048]
                    e[name + "_omitted_chars"] = len(value) - 2048
            events.append(e)
            scope["events_projected"] += 1
        if error:
            add("gap", reason=error)
            continue
        if profile is None:
            scope["unknown_records"] += 1
            add("unknown", reason="harness_not_recognized")
            continue
        session = profile["session"]
        if not external_sid and matches(session.get("when", []), record, record):
            external_sid = first(session.get("external_id", []), record)
            version = version or first(session.get("harness_version", []), record)
        if parent_session is None and session.get("parent") and matches(session.get("parent_when", []), record, record):
            # A recorded subagent or forked session: its requests may come from the parent, not a person.
            parent_session = first(session["parent"], record)
        for rule, item, key, ptr in interpret(profile["records"], record, record):
            kind = rule["emit"]
            if kind == "boundary":
                add("boundary", reason="recorded_compaction_or_summary; contents_not_promoted", pointer=ptr)
                continue
            if kind == "ignore":
                scope["ignored_items"] += 1
                continue
            if kind == "unknown":
                scope["unknown_records"] += 1
                add("unknown", reason="profile_does_not_interpret_item", pointer=ptr)
                continue
            text = field(rule.get("text"), item, key)
            if text is None and rule.get("text_fallback"):
                text = field(rule.get("text_fallback"), item, key)
            if not text and rule.get("json") is not None:
                spec = rule["json"]
                if isinstance(spec, dict):
                    data = {name: pointer(item, path) for name, path in spec.items()}
                elif isinstance(spec, list):
                    data = {path.strip("/").replace("/", "_"): pointer(item, path) for path in spec}
                else:
                    data = pointer(item, spec)
                text = json.dumps(data, ensure_ascii=True)
            kwargs = {"pointer": ptr}
            for name in ("call_id", "tool", "exit_code", "command", "target_id", "replacement", "first_kept_id"):
                val = field(rule.get(name), item, key)
                if val is not None:
                    kwargs[name] = val
            if rule.get("representation"):
                kwargs["representation"] = rule["representation"]
            for name in ("origin", "channel", "purpose", "state", "text_state", "role"):
                if rule.get(name) is not None:
                    kwargs[name] = rule[name]
            kwargs["outcome"] = "failed" if rule.get("failed_when") and matches(rule["failed_when"], item, record) else (
                "succeeded" if rule.get("succeeded_when") and matches(rule["succeeded_when"], item, record) else "unknown")
            spec = rule.get("text")
            payload_ptr = ptr + (spec if isinstance(spec, str) else spec.get("pointer", "") if spec else "")
            if kind == "tool_call":
                data = field(rule.get("input"), item, key)
                tool = profile["tools"].get(kwargs.get("tool"), {})
                kwargs["operation"] = tool.get("operation", "unknown")
                for name in ("path", "command", "cwd"):
                    val = field(tool.get(name), data)
                    if val is not None:
                        kwargs[name] = val
                if isinstance(data, dict):
                    text = json.dumps(data, ensure_ascii=True)
                elif isinstance(data, str):
                    text = data
                inp = rule.get("input")
                payload_ptr = ptr + (inp if isinstance(inp, str) else inp.get("pointer", "") if inp else "")
            if kind == "tool_result":
                payload_ptr = ptr + rule.get("content", "/content")
                text = content_text(pointer(record, payload_ptr))
            if kind in ("command", "file_read", "file_edit"):
                if kind == "command":
                    text = kwargs.get("command", "")
                    payload_ptr = ptr + rule["content"] if rule.get("content") else ptr
                else:
                    kwargs["path"] = field(rule.get("path"), item, key)
                kwargs["operation"] = kind
            if "path" in kwargs:
                kwargs["path_raw"] = kwargs["path"]
                kwargs["path"], kwargs["path_location"] = resolve_path(kwargs["path"], kwargs.get("cwd") or root, root)
            add(kind, text or "", payload_pointer=payload_ptr, **kwargs)
    # Merge only records that prove the same message. Distinct representations stay.
    def same(a, b):
        return a.get("text_hash") and a.get("text_hash") == b.get("text_hash")
    groups = {}
    for e in events:
        if e["kind"] in ("user_message", "assistant_message") and e.get("external_message_id"):
            groups.setdefault((e["kind"], e["external_message_id"]), []).append(e)
    removed = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        if all(same(group[0], other) for other in group[1:]):
            for other in group[1:]:
                removed.add(id(other))
                scope["duplicates_skipped"] += 1
        else:
            scope["representation_variants"] += len(group) - 1
            scope["mixed_representations"] = True
    # Without IDs, only an adjacent echo with the same parent/turn and text is proven.
    previous = None
    for e in events:
        if e["kind"] in ("user_message", "assistant_message") and not e.get("external_message_id"):
            previous_parent = previous and (previous.get("external_parent_id") or previous.get("external_turn_id"))
            current_parent = e.get("external_parent_id") or e.get("external_turn_id")
            if (previous is not None and previous["kind"] == e["kind"] and same(previous, e)
                    and previous_parent and previous_parent == current_parent):
                removed.add(id(e))
                scope["duplicates_skipped"] += 1
                continue
        previous = e
    # Declared representations of one message: consecutive messages of one kind with identical
    # text, different representation labels and at most one message id. Keep the id-bearing row.
    last = None
    for e in events:
        if e["kind"] not in ("user_message", "assistant_message") or id(e) in removed:
            continue
        if (last is not None and last["kind"] == e["kind"] and same(last, e)
                and last.get("representation") and e.get("representation")
                and last["representation"] != e["representation"]
                and not (last.get("external_message_id") and e.get("external_message_id"))):
            removed.add(id(last if e.get("external_message_id") else e))
            scope["duplicates_skipped"] += 1
            last = None
            continue
        last = e
    if removed:
        events = [e for e in events if id(e) not in removed]
    # Every row of a recording carries one session identity, even rows read before it appeared.
    sid = str(external_sid) if external_sid is not None else sid
    parent_session = str(parent_session) if parent_session else None
    for e in events:
        e["session"] = sid
        if parent_session:
            e["parent_session"] = parent_session
    if parent_session:
        scope["recordings_with_parent"] += 1
    calls = {}
    messages = {}
    current_turn = None
    for e in events:
        mid, parent = e.get("external_message_id"), e.get("external_parent_id")
        if e["kind"] == "user_message":
            current_turn = e.get("external_turn_id") or mid or f"{e['ref']['line']}:{e['ref']['item']}"
        if parent:
            turn = messages.get(parent)
            e["turn_basis"] = "recorded_ancestry" if turn else "unresolved_ancestry"
            e["turn"] = current_turn if e["kind"] == "user_message" else turn
            if not turn and e["kind"] != "user_message":
                scope["ancestry_unknown"] += 1
        else:
            e["turn"] = e.get("external_turn_id") or current_turn
            e["turn_basis"] = "recorded_turn" if e.get("external_turn_id") else "record_order"
        if mid and e.get("turn"):
            messages[mid] = e["turn"]
        if e["kind"] == "tool_call" and e.get("call_id"):
            calls.setdefault(e["call_id"], []).append(e)
    scope["ambiguous_call_ids"] += sum(len(v) > 1 for v in calls.values())
    # Pi chains parallel results: each result's parent is the previous result, not the call's message.
    result_parents = {e["external_message_id"]: e.get("external_parent_id") for e in events
                      if e["kind"] == "tool_result" and e.get("external_message_id")}

    def recorded_under(result, message_id):
        parent, seen = result.get("external_parent_id"), set()
        while parent and parent != message_id and parent in result_parents and parent not in seen:
            seen.add(parent)
            parent = result_parents[parent]
        return parent == message_id

    for e in events:
        if e["kind"] != "tool_result":
            continue
        candidates = calls.get(e.get("call_id"), [])
        if len(candidates) == 1 and candidates[0]["order"] <= e["order"]:
            call = candidates[0]
            if "result_ref" in call or (e.get("external_parent_id") and call.get("external_message_id")
                                        and not recorded_under(e, call["external_message_id"])):
                scope["unpaired_results"] += 1
                continue
            call["result_ref"] = e["ref"]
            call["outcome"] = e["outcome"]
            if "exit_code" in e:
                call["exit_code"] = e["exit_code"]
            e["call_ref"] = call["ref"]
            for k in ("operation", "tool", "path", "path_raw", "path_location", "command"):
                if k in call:
                    e[k] = call[k]
        else:
            scope["unpaired_results"] += 1
    scope["calls_without_result"] += sum(e["kind"] == "tool_call" and "result_ref" not in e for e in events)
    return {"file": str(Path(path).absolute()), "session": sid, "parent_session": parent_session, "root": root,
            "harness_version": version, "sampled": bool(ranges),
            "harness": profile["harness"] if profile else "unknown", "events": events}


__all__ = ["scan_file", "safe_ref", "PROFILES", "detected", "recorded_root", "pointer",
           "content_text", "field", "first", "interpret", "matches", "resolve_path"]
