"""Replay recorded request context without claiming unrecorded provider input."""
import hashlib

from .profiles import content_text


def _find(events, ref):
    return next((e for e in events if e["ref"]["offset"] == ref["offset"]
                 and e["ref"].get("item", 0) == ref.get("item", 0)), None)


def _item(event, ordinal, included=True, transformation="as_recorded"):
    return {"request_ref": None, "ordinal": ordinal, "container_path": event.get("ref", {}).get("pointer", ""),
            "role": event.get("role"), "origin": event.get("origin"), "block_type": event.get("kind"),
            "content_state": event.get("text_state") or event.get("state") or "absent",
            "source_ref": event.get("ref"), "content_hash": event.get("text_hash"),
            "transformation": transformation, "included": included,
            "event_id": event.get("event_id")}


def context_at_request(events, target_ref):
    """Build the best recorded context for one request; exactness is a separate claim."""
    target = _find(events, target_ref)
    if target is None:
        return {"guarantee": "unavailable", "items": [], "dropped": [], "request_ref": target_ref,
                "reason": "request event not found within scan bounds"}
    by_id = {}
    for event in events:
        mid = event.get("external_message_id")
        if mid and mid not in by_id:
            by_id[mid] = event
    chain, seen = [], set()
    current = target
    while current is not None:
        key = id(current)
        if key in seen:
            break
        seen.add(key)
        chain.append(current)
        parent = current.get("external_parent_id")
        current = by_id.get(parent) if parent else None
    chain.reverse()
    if not chain:
        chain = [e for e in events if e.get("order", 0) <= target.get("order", 0)]
    # Context edits replace an ancestor's recorded content.
    replaced = {}
    for event in events:
        if event.get("kind") != "context_change" or event.get("order", 0) > target.get("order", 0):
            continue
        tid = event.get("target_id")
        if not tid:
            continue
        replacement = event.get("replacement")
        if isinstance(replacement, list):
            replacement = content_text(replacement)
        replaced[tid] = (replacement, event)
    effective, transformations = [], 0
    for event in chain:
        tid = event.get("external_message_id")
        if tid in replaced:
            replacement, change = replaced[tid]
            copy = dict(event)
            if isinstance(replacement, str):
                copy["text"] = replacement
                copy["text_chars"] = len(replacement)
                copy["text_hash"] = hashlib.sha256(replacement.encode("utf-8")).hexdigest()
            effective.append(copy)
            transformations += 1
            replaced[tid] = (replacement, change, event)
        else:
            effective.append(event)
    # A recorded compaction can drop everything before its first kept entry.
    summaries = [e for e in events if e.get("kind") == "summary"
                 and e.get("purpose") == "compaction" and e.get("order", 0) <= target.get("order", 0)]
    dropped = []
    guarantee = "replayed" if transformations else "transcript_only"
    if summaries:
        summary = summaries[-1]
        first = summary.get("first_kept_id")
        if first:
            index = next((i for i, e in enumerate(effective)
                          if e.get("external_message_id") == first), None)
            if index is not None:
                dropped = effective[:index]
                effective = [summary] + effective[index:]
                transformations += 1
                guarantee = "replayed"
            else:
                effective = [summary] + effective
                guarantee = "transcript_only"
        else:
            effective = [summary] + effective
            guarantee = "transcript_only"
    allowed = {"user_message", "assistant_message", "summary", "instruction", "attachment", "reasoning"}
    effective = [e for e in effective if e.get("kind") in allowed]
    dropped = [e for e in dropped if e.get("kind") in allowed]
    items = []
    for ordinal, event in enumerate(effective):
        row = _item(event, ordinal, True, "replayed" if transformations else "as_recorded")
        row["request_ref"] = target["ref"]
        # Keep replacement history attached to the effective component.
        tid = event.get("external_message_id")
        if tid in replaced and len(replaced[tid]) == 3:
            row["transformation"] = "replaced_by_context_edit"
            row["transformation_ref"] = replaced[tid][1]["ref"]
        items.append(row)
    dropped_rows = []
    for ordinal, event in enumerate(dropped):
        row = _item(event, ordinal, False, "dropped_by_compaction")
        row["request_ref"] = target["ref"]
        dropped_rows.append(row)
    if target.get("harness") == "claude-code":
        guarantee = "transcript_only"
    return {"guarantee": guarantee, "items": items, "dropped": dropped_rows,
            "request_ref": target["ref"], "relationship_basis": "recorded ancestry" if by_id else "record order"}


def changed_components(left, right):
    """Compare two replayed request contexts by identity, preserving removals."""
    left_map = {(row["block_type"], row.get("content_hash") or row["ordinal"]): row for row in left["items"]}
    right_map = {(row["block_type"], row.get("content_hash") or row["ordinal"]): row for row in right["items"]}
    rows = []
    for key in sorted(set(left_map) | set(right_map), key=str):
        before, after = left_map.get(key), right_map.get(key)
        if before and not after:
            rows.append({**before, "change": "removed"})
        elif after and not before:
            rows.append({**after, "change": "added"})
        elif before.get("content_hash") != after.get("content_hash"):
            rows.append({**after, "change": "replaced", "before_ref": before.get("source_ref")})
    return rows
