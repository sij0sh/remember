"""Bounded JSONL scan: profiles produce events; bytes stay in the source."""
import hashlib
import json
import re
from pathlib import Path

from .profiles import (PROFILES, content_text, detected, field, first, interpret,
                       matches, pointer, recorded_root, resolve_path, stamp)
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


def message_graph(events):
    """Recorded message ancestry: duplicate-aware parents, nearest-first chains and cycle status.

    Message identity is the harness's original message ID. Items of one message share it.
    Two records claiming the same ID at different offsets are ambiguous, not one message.
    """
    by_id = {}
    for event in events:
        message_id = event.get("external_message_id")
        if message_id:
            by_id.setdefault(message_id, []).append(event)

    def offsets(message_id):
        return {event.get("ref", {}).get("offset") for event in by_id.get(message_id, [])}

    def ambiguous(message_id):
        return len(offsets(message_id)) > 1

    def parent(message_id):
        group = by_id.get(message_id) or []
        parents = {event.get("external_parent_id") for event in group if event.get("external_parent_id")}
        if len(parents) > 1:
            return None, "ambiguous"
        return (next(iter(parents)) if parents else None), "ok"

    cache = {}

    def chain(message_id):
        """Strict ancestor message IDs, nearest first; ('root'|'missing'|'cycle'|'ambiguous') status."""
        if message_id in cache:
            return cache[message_id]
        found, seen, current = [], set(), message_id
        while True:
            if not current or ambiguous(current):
                result = (found, "ambiguous") if current else (found, "root")
                break
            upstream, state = parent(current)
            if state != "ok":
                result = (found, "ambiguous")
                break
            if not upstream:
                result = (found, "root")
                break
            if upstream in seen:
                result = (found, "cycle")
                break
            seen.add(upstream)
            if upstream not in by_id:
                result = (found, "missing")
                break
            found.append(upstream)
            current = upstream
        cache[message_id] = result
        return result

    def is_ancestor(ancestor, message_id):
        """(True|False, status) for a strict ancestor; unsupported ancestry is never a negative proof."""
        ancestors, status = chain(message_id)
        return (ancestor in ancestors), status

    def end_event(message_id):
        """The last recorded content item of one unambiguous message, else None."""
        if not message_id or ambiguous(message_id):
            return None
        group = by_id.get(message_id) or []
        return max(group, key=lambda event: (event.get("order") or 0, event.get("ref", {}).get("item", 0))) if group else None

    return {"by_id": by_id, "offsets": offsets, "ambiguous": ambiguous, "parent": parent,
            "chain": chain, "is_ancestor": is_ancestor, "end_event": end_event}


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
    linear_parent = None
    # Recorded ancestry of every readable record, including records with no projected event
    # (for example an empty assistant error record). Citation labels are branch positions:
    # they need the full recorded chain, not only the rows a profile chose to emit.
    record_index, record_conflicts = {}, set()
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
        record_parent = None
        if record is not None and profile and profile.get("correlation", {}).get("external_parent_id"):
            correlation = profile.get("correlation", {})
            identity = first(correlation.get("external_message_id", []), record)
            if identity:
                parent = first(correlation.get("external_parent_id", []), record)
                prior = record_index.get(identity)
                if prior is not None and (prior.get("parent") != parent or prior.get("offset") != ref.get("offset")):
                    record_conflicts.add(identity)
                else:
                    record_index.setdefault(identity, {"parent": parent, "ref": safe_ref(ref), "offset": ref.get("offset")})
        if record is not None and profile and profile.get("ancestry"):
            ancestry = profile["ancestry"].get("linear")
            identity = first(profile.get("correlation", {}).get("external_message_id", []), record)
            if identity and (ancestry is True or matches(ancestry or [], record, record)):
                # A linear log records no parent ids: the previous message record is this one's parent.
                record_parent, linear_parent = linear_parent, identity
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
                    "assistant_message", "summary", "reasoning", "checkpoint") else "system" if kind in (
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
                correlation = profile.get("correlation", {})
                applies = matches(correlation.get("when", []), record, record)
                for key, specs in correlation.items():
                    if key != "when":
                        e[key] = first(specs, record) if applies else None
                e["timestamp"] = stamp(profile.get("timestamp", []), record)
                if record_parent and not e.get("external_parent_id"):
                    e["external_parent_id"] = record_parent
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
            if kind not in ("gap", "unknown"):
                scope["items_interpreted"] += 1
            events.append(e)
            scope["events_projected"] += 1
        if error:
            if error in ("malformed_record", "oversized_record"):
                scope["interpretation_gaps"] += 1
            add("gap", reason=error)
            continue
        if profile is None:
            scope["unknown_records"] += 1
            scope["interpretation_unsupported"] += 1
            add("unknown", reason="harness_not_recognized")
            continue
        session = profile["session"]
        if not external_sid and matches(session.get("when", []), record, record):
            external_sid = first(session.get("external_id", []), record)
            version = version or first(session.get("harness_version", []), record)
        if parent_session is None and session.get("parent") and matches(session.get("parent_when", []), record, record):
            # A recorded subagent or forked session: its requests may come from the parent, not a person.
            parent_session = first(session["parent"], record)
        before_items = scope["events_projected"]
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
                scope["interpretation_unsupported"] += 1
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
            for name in ("call_id", "tool", "exit_code", "command", "target_id", "replacement",
                         "first_kept_id", "coverage_end_id"):
                val = field(rule.get(name), item, key)
                if val is not None:
                    kwargs[name] = val
            if rule.get("representation"):
                kwargs["representation"] = rule["representation"]
            if rule.get("coverage_end") is not None:
                kwargs["coverage_end_id"] = field(rule["coverage_end"], item, key)
            # Named scalar/list projections for one record (for example envelope
            # versions, producer ids and file lists that no event column owns).
            for name, spec in (rule.get("fields") or {}).items():
                val = field(spec, item, key)
                if val is not None:
                    kwargs[name] = val
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
        if scope["events_projected"] == before_items and not error and not scope.get("event_limit_reached"):
            scope["records_unrepresented"] += 1
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
    # Physical order is (byte offset, item index); `order` is a comparable sequence
    # over the scanned range so head and tail rows share one unit.
    for index, e in enumerate(events):
        # `order` is the internal sequence; `seq` is its public SQL-safe name.
        e["order"] = e["seq"] = index
        e["byte_offset"] = e["ref"]["offset"]
        e["item_index"] = e["ref"].get("item", 0)
        e["position"] = [e["byte_offset"], e["item_index"]]
    calls = {}
    by_mid = {}
    for e in events:
        mid = e.get("external_message_id")
        if mid:
            by_mid.setdefault(mid, []).append(e)

    def representative(group):
        for kind in ("user_message", "assistant_message", "summary", "tool_result", "tool_call"):
            found = next((x for x in group if x["kind"] == kind), None)
            if found is not None:
                return found
        return group[0]

    # Groups are complete here and kinds never change, so each group's representative
    # and ambiguity are fixed: compute them once instead of on every ancestry step.
    rep_by_mid = {mid: representative(group) for mid, group in by_mid.items()}
    ambiguous_mids = {mid for mid, group in by_mid.items()
                      if len({item.get("ref", {}).get("offset") for item in group}) > 1}
    ancestry_cache = {}

    def walk(start):
        """Full walk from one parent id; used where the walk meets a cycle."""
        chain, seen, parent = [], set(), start
        while parent:
            if parent in seen:
                found = (chain, "cycle")
                ancestry_cache[start] = found
                return found
            seen.add(parent)
            if parent not in rep_by_mid:
                found = (chain, "missing")
                ancestry_cache[start] = found
                return found
            if parent in ambiguous_mids:
                # The same native message ID at different records is not one proven ancestor.
                found = (chain, "ambiguous")
                ancestry_cache[start] = found
                return found
            event = rep_by_mid[parent]
            chain.append(event)
            parent = event.get("external_parent_id")
        found = (chain, "root")
        ancestry_cache[start] = found
        return found

    def ancestry(e):
        """Nearest-first recorded ancestors and whether the chain reached a root.
        Off a cycle, a parent's result is its representative plus its own parent's result,
        so each id is walked once and later chains extend cached ones."""
        start = e.get("external_parent_id")
        if not start:
            return [], "root"
        if start in ancestry_cache:
            return ancestry_cache[start]
        path, seen, parent = [], set(), start
        while True:
            if parent in ancestry_cache:
                tail = ancestry_cache[parent]
                break
            if parent in seen:
                return walk(start)
            seen.add(parent)
            if parent not in rep_by_mid:
                tail = ([], "missing")
                break
            if parent in ambiguous_mids:
                tail = ([], "ambiguous")
                break
            path.append(parent)
            parent = rep_by_mid[parent].get("external_parent_id")
            if not parent:
                tail = ([], "root")
                break
        chain, status = tail
        if not path:
            ancestry_cache[start] = tail
        for mid in reversed(path):
            chain = [rep_by_mid[mid]] + chain
            ancestry_cache[mid] = (chain, status)
        return ancestry_cache[start]

    def human_user(x):
        # A parent-session prompt is delegated by metadata, not proven human input.
        return (x.get("kind") == "user_message" and x.get("origin", "human") == "human"
                and not x.get("parent_session"))

    def turn_id(x):
        return x.get("external_turn_id") or x.get("external_message_id") or \
            f"{x['ref']['offset']}:{x['ref'].get('item', 0)}"

    current_turn = None
    for e in events:
        chain, status = ancestry(e)
        if status in ("missing", "cycle", "ambiguous"):
            scope["ancestry_unknown"] += 1
            if status == "cycle":
                scope["ancestry_cycles"] += 1
            if status == "ambiguous":
                scope["ancestry_ambiguous"] = scope.get("ancestry_ambiguous", 0) + 1
        own = e if e.get("kind") == "user_message" else None
        candidates = ([own] if own is not None else []) + chain
        request = next((x for x in candidates if x.get("kind") == "user_message"), None)
        human_request = next((x for x in candidates if human_user(x)), None)
        chosen = human_request or request
        if human_request is not None:
            e["request_origin"] = "human"
        elif request is not None:
            e["request_origin"] = "harness" if request.get("origin") == "harness" else "delegated"
        else:
            e["request_origin"] = "unknown"
        if chosen is not None:
            e["request_ref"] = chosen["ref"]
            e["request_basis"] = ("self" if chosen is e else "unresolved_ancestry"
                                  if status in ("missing", "cycle", "ambiguous") else "recorded_ancestry")
            e["turn"] = turn_id(chosen)
            e["turn_basis"] = "self" if chosen is e else ("unresolved_ancestry"
                                                          if status in ("missing", "cycle", "ambiguous")
                                                          else "recorded_ancestry")
        elif e.get("external_turn_id"):
            e["turn"] = e["external_turn_id"]
            e["turn_basis"] = "recorded_turn"
            e["request_basis"] = "recorded_turn" if status == "root" else status
        elif status == "root" and current_turn is not None:
            e["turn"] = current_turn
            e["turn_basis"] = "record_order"
            e["request_basis"] = "record_order"
        else:
            e["turn"] = None
            e["turn_basis"] = "unresolved_ancestry" if status in ("missing", "cycle", "ambiguous") else "record_order"
            e["request_basis"] = status if status in ("missing", "cycle", "ambiguous") else "unresolved"
        # Lane is the conversation lane of the nearest supported request. A follow-up under
        # an assistant/result continues that lane; a user message parented to a user starts
        # a sibling lane. Descendants inherit the request's lane, so they cannot split off.
        parent_message = rep_by_mid.get(e.get("external_parent_id")) if e.get("external_parent_id") else None
        if own is not None and human_user(own):
            if parent_message is not None and parent_message.get("kind") != "user_message" and parent_message.get("lane"):
                lane, lane_basis = parent_message["lane"], "continued_exchange"
            else:
                lane, lane_basis = turn_id(own), "self"
        elif human_request is not None:
            lane, lane_basis = human_request.get("lane") or turn_id(human_request), "recorded_ancestry"
        elif request is not None:
            lane, lane_basis = request.get("lane") or turn_id(request), "recorded_ancestry"
        else:
            lane, lane_basis = turn_id(e), "unresolved"
        e["lane"], e["lane_basis"] = lane, lane_basis
        # Branch is the top-level segment under the exchange request: sibling descendants
        # (parallel assistant messages, forks) stay separate even inside one lane.
        exchange_root = human_request or request
        branch_basis = "self"
        if exchange_root is None:
            branch_key = turn_id(e)
            branch_basis = "unresolved"
        elif exchange_root is e:
            branch_key = turn_id(e)
        else:
            at = next((index for index, item in enumerate(chain)
                       if item is exchange_root or (item.get("external_message_id")
                                                    and item.get("external_message_id") == exchange_root.get("external_message_id"))), None)
            if at is None:
                branch_key, branch_basis = turn_id(e), "unresolved_ancestry"
            elif at == 0:
                branch_key = turn_id(e)
            else:
                branch_key, branch_basis = turn_id(chain[at - 1]), "recorded_ancestry"
        e["branch"], e["branch_basis"] = branch_key, branch_basis
        e["message_id"] = e.get("external_message_id") or turn_id(e)
        e["ancestry_status"] = status if e.get("external_parent_id") else "root"
        e["ancestry_depth"] = len(chain)
        if e.get("external_message_id"):
            e["message_ambiguous"] = e["external_message_id"] in ambiguous_mids
            if e["message_ambiguous"]:
                scope["ambiguous_message_ids"] = scope.get("ambiguous_message_ids", 0) + 1
        if own is not None and human_user(own):
            current_turn = turn_id(own)
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
            for k in ("operation", "tool", "path", "path_raw", "path_location", "command", "request_ref",
                      "request_basis", "request_origin", "turn", "lane"):
                if k in call:
                    e[k] = call[k]
        else:
            scope["unpaired_results"] += 1
    scope["calls_without_result"] += sum(e["kind"] == "tool_call" and "result_ref" not in e for e in events)
    return {"file": str(Path(path).absolute()), "session": sid, "parent_session": parent_session, "root": root,
            "harness_version": version, "sampled": bool(ranges),
            "harness": profile["harness"] if profile else "unknown", "events": events,
            "record_ancestry": record_index, "record_conflicts": record_conflicts}


__all__ = ["scan_file", "safe_ref", "PROFILES", "detected", "recorded_root", "pointer",
           "content_text", "field", "first", "interpret", "matches", "resolve_path"]
