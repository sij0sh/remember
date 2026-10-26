"""Recorded edit hunks and the definitions they changed, from already-scanned events.

Each harness records line-numbered hunks somewhere different: Claude Code in the
tool result's ``structuredPatch`` (with the pre-edit ``originalFile``), Codex in a
FileChange item's ``unified_diff``, pi in the edit result's ``details.patch``; pi
``write`` and file creations carry whole content. Recorded line numbers belong to
the file as it was at the edit, so symbols come from one stated basis per hunk:

- ``at_edit``: the pre-edit file was recorded; the patch is replayed and the
  post-edit file parsed, naming what the edit changed at that moment.
- ``current``: the hunk's new-side text is found, unchanged and unique, in today's
  file, and today's ``code_items`` give the definitions at those lines.
- ``none``: neither applies. Nothing is guessed from a stale line number.
"""
import re

from . import repo_files
from .profiles import pointer

HUNK_GRAIN = "one recorded diff hunk of one file edit operation"
SYMBOL_GRAIN = "one definition whose lines a recorded hunk changed"
HUNK_COLUMNS = {"recording_id": "VARCHAR", "event_id": "VARCHAR", "session": "VARCHAR",
                "independent_session": "VARCHAR", "harness": "VARCHAR",
                "timestamp": "VARCHAR", "tool": "VARCHAR", "repo_id": "VARCHAR", "path": "VARCHAR",
                "location": "VARCHAR", "recorded_path": "VARCHAR",
                "hunk": "BIGINT", "change": "VARCHAR", "source": "VARCHAR",
                "old_start": "BIGINT", "old_lines": "BIGINT", "new_start": "BIGINT", "new_lines": "BIGINT",
                "added": "BIGINT", "removed": "BIGINT", "changed_start": "BIGINT", "changed_end": "BIGINT",
                "anchor": "VARCHAR", "current_start": "BIGINT",
                "current_end": "BIGINT", "symbol_basis": "VARCHAR", "ref": "JSON"}
SYMBOL_COLUMNS = {"recording_id": "VARCHAR", "event_id": "VARCHAR", "hunk": "BIGINT", "repo_id": "VARCHAR",
                  "path": "VARCHAR", "basis": "VARCHAR", "kind": "VARCHAR", "name": "VARCHAR",
                  "qualname": "VARCHAR", "parent": "VARCHAR", "line": "BIGINT", "end_line": "BIGINT",
                  "changed_lines": "BIGINT", "innermost": "BOOLEAN", "parse_state": "VARCHAR"}
HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def split_lines(text):
    return text.split("\n")[:-1] if text.endswith("\n") else text.split("\n") if text else []


def parse_unified(diff):
    """[(old_start, old_lines, new_start, new_lines, lines)] from unified diff text.

    A hunk's stated line counts bound its body, so file headers and trailing text
    are never read as changes; a blank body line is context whose space was stripped.
    """
    hunks, old_left, new_left = [], 0, 0
    for line in (diff or "").split("\n"):
        found = HEADER.match(line)
        if found:
            old, old_n, new, new_n = found.groups()
            old_left, new_left = int(old_n or 1), int(new_n or 1)
            hunks.append((int(old), old_left, int(new), new_left, []))
            continue
        if not hunks:
            continue
        tag = line[:1] or " "
        if tag == "\\" and hunks[-1][4]:
            hunks[-1][4].append(line)
        elif old_left > 0 or new_left > 0:
            if tag == " " and old_left > 0 and new_left > 0:
                old_left, new_left = old_left - 1, new_left - 1
            elif tag == "-" and old_left > 0:
                old_left -= 1
            elif tag == "+" and new_left > 0:
                new_left -= 1
            else:
                old_left = new_left = 0  # malformed: stop this hunk
                continue
            hunks[-1][4].append(line if line else " ")
    return hunks


def _whole(content, sign):
    lines = split_lines(content or "")
    return [(0, 0, 1, len(lines), [sign + l for l in lines])] if sign == "+" else \
           [(1, len(lines), 0, 0, [sign + l for l in lines])]


def shape(lines):
    """(new-side block, changed indexes into it, added, removed) for one hunk's lines."""
    block, changed, added, removed, pending = [], [], 0, 0, False
    for line in lines:
        tag, body = (line[:1] or " "), line[1:]
        if tag == "\\":
            continue
        if tag == "-":
            removed, pending = removed + 1, True
            continue
        if tag == "+":
            added += 1
            changed.append(len(block))
        elif pending:
            changed.append(len(block))  # a deletion lands just before this line
        pending = False
        block.append(body)
    if pending and block:
        changed.append(len(block) - 1)
    return block, sorted(set(changed)), added, removed


def replay(original, hunks):
    """The post-edit text from the pre-edit text and its hunks, or None if context disagrees."""
    source, out, at = split_lines(original), [], 0
    for old_start, old_lines, _, _, lines in hunks:
        start = max(old_start - 1, 0) if old_lines else old_start  # an insertion follows old_start
        if start < at or start > len(source):
            return None
        out.extend(source[at:start])
        at = start
        for line in lines:
            tag, body = line[:1] or " ", line[1:]
            if tag == "\\":
                continue
            if tag in (" ", "-"):
                if at >= len(source) or source[at].rstrip("\r") != body.rstrip("\r"):
                    return None
                at += 1
            if tag in (" ", "+"):
                out.append(body)
    out.extend(source[at:])
    return "\n".join(out) + ("\n" if original.endswith("\n") or not original else "")


def anchor(block, new_start, current):
    """(state, zero-based offset of the block in today's lines or None)."""
    if not block:
        return "no_new_text", None
    size, start = len(block), max(new_start - 1, 0)
    if [l.rstrip("\r") for l in current[start:start + size]] == [l.rstrip("\r") for l in block]:
        return "exact", start
    first = block[0].rstrip("\r")
    hits = [i for i, line in enumerate(current) if line.rstrip("\r") == first
            and [l.rstrip("\r") for l in current[i:i + size]] == [l.rstrip("\r") for l in block]]
    if len(hits) == 1:
        return "shifted", hits[0]
    return ("ambiguous" if hits else "not_found"), None


def _claude(event, record):
    result = record.get("toolUseResult")
    if not isinstance(result, dict):
        return None, "no_structured_result"
    if result.get("type") == "create":
        return ("add", "claude_structured_patch", _whole(result.get("content"), "+"), None,
                result.get("content")), None
    patch = result.get("structuredPatch")
    if not isinstance(patch, list):
        return None, "no_structured_result"
    hunks = [(h.get("oldStart", 0), h.get("oldLines", 0), h.get("newStart", 0), h.get("newLines", 0),
              list(h.get("lines") or [])) for h in patch if isinstance(h, dict)]
    original = result.get("originalFile")
    after = result.get("content") if isinstance(result.get("content"), str) else None
    if after is None and isinstance(original, str):
        after = replay(original, hunks)
    return ("update", "claude_structured_patch", hunks, original, after), None


def _codex(event, record):
    change = pointer(record, event.get("payload_pointer"))
    if not isinstance(change, dict):
        return None, "no_structured_result"
    kind = change.get("type")
    if kind == "update":
        return ("update", "codex_unified_diff", parse_unified(change.get("unified_diff")), None, None), None
    if kind == "add":
        return ("add", "codex_content", _whole(change.get("content"), "+"), None, change.get("content")), None
    if kind == "delete":
        return ("delete", "codex_content", _whole(change.get("content"), "-") if change.get("content") else [],
                None, None), None
    return None, "unrecognized_change"


def _pi(event, record):
    if event.get("tool") == "write":
        arguments = pointer(record, event.get("payload_pointer"))
        content = arguments.get("content") if isinstance(arguments, dict) else None
        if not isinstance(content, str):
            return None, "no_structured_result"
        return ("write", "pi_write_content", _whole(content, "+"), None, content), None
    details = pointer(record, "/message/details")
    patch = details.get("patch") if isinstance(details, dict) else None
    if not isinstance(patch, str):
        return None, "no_structured_result"
    return ("update", "pi_patch", parse_unified(patch), None, None), None


def _extractor(event):
    """Which event of an operation carries its hunks; the other half is never read."""
    harness, kind, tool = event.get("harness"), event.get("kind"), event.get("tool")
    if event.get("operation") != "file_edit":
        return None
    if harness == "claude-code" and kind == "tool_result" and tool in ("Edit", "MultiEdit", "Write"):
        return _claude
    if harness == "codex" and kind == "file_edit":
        return _codex
    if harness == "pi" and ((kind == "tool_result" and tool == "edit") or (kind == "tool_call" and tool == "write")):
        return _pi
    return None


def _overlaps(items, changed):
    """[(item, changed lines inside, innermost)] for definitions covering any changed line."""
    covering = [(item, [n for n in changed if item["line"] <= n <= (item["end_line"] or item["line"])])
                for item in items if item.get("kind") != "import"]
    covering = [(item, inside) for item, inside in covering if inside]
    innermost = set()
    for n in changed:
        holders = [item for item, inside in covering if n in inside]
        if holders:
            best = min(holders, key=lambda i: ((i["end_line"] or i["line"]) - i["line"], -i["line"]))
            innermost.add(id(best))
    return [(item, len(inside), id(item) in innermost) for item, inside in covering]


def derive(recordings, cache, repository):
    """(hunks, symbols, coverage) for every scanned edit operation, before event filters."""
    hunks, symbols = [], []
    coverage = {"complete": True, "operations": 0, "operations_with_hunks": 0, "failed_skipped": 0,
                "without_hunks": {}, "at_edit_unavailable": {}, "hunks": 0, "anchors": {}, "symbol_basis": {},
                "locations": {}}
    for recording in recordings:
        for event in recording["events"]:
            extract = _extractor(event)
            if extract is None:
                continue
            coverage["operations"] += 1
            if event.get("outcome") == "failed":
                coverage["failed_skipped"] += 1
                continue
            try:
                found, reason = extract(event, cache.get(event["ref"]))
            except (OSError, ValueError, KeyError) as error:
                found, reason = None, f"unreadable: {type(error).__name__}"
            if not found or not found[2]:
                reason = reason or "no_hunks"
                coverage["without_hunks"][reason] = coverage["without_hunks"].get(reason, 0) + 1
                continue
            change, source, recorded, original, after = found
            coverage["operations_with_hunks"] += 1
            path = repository.repo_path(event)
            recorded_path = event.get("path_raw") or event.get("path")
            # Rank churn on repository rows; scratch files elsewhere keep their recorded path.
            location = "repository" if path else "outside_repo" if recorded_path else "unknown"
            current, unavailable = repository.current_lines(path)
            at_edit, why = None, None
            if after is None:
                why = "replay_mismatch" if isinstance(original, str) else "post_edit_file_not_recorded"
            elif not path:
                why = "outside_repo"
            else:
                data = after.encode("utf-8", "replace")
                language, ext = repo_files.language_of(path, data)
                rows, state, _ = repo_files.parse_symbols(data, after, repository.repo_id, path, language, ext)
                at_edit = (rows, state)
                if state not in ("parsed", "parsed_with_errors"):
                    why = state
            if why:
                coverage["at_edit_unavailable"][why] = coverage["at_edit_unavailable"].get(why, 0) + 1
            for index, (old_start, old_lines, new_start, new_lines, lines) in enumerate(recorded):
                block, changed, added, removed = shape(lines)
                recorded_changed = [new_start + i if block else max(new_start, 1) for i in (changed or [0])]
                state, offset = anchor(block, new_start, current) if current is not None else (unavailable, None)
                live = [offset + 1 + i for i in changed] if offset is not None else None
                if at_edit and at_edit[1] in ("parsed", "parsed_with_errors"):
                    basis, items, lines_at, parse_state = "at_edit", at_edit[0], recorded_changed, at_edit[1]
                elif live and repository.has_items(path):
                    basis, items, lines_at, parse_state = "current", repository.items_for(path), live, None
                else:
                    basis, items, lines_at, parse_state = "none", [], [], None
                coverage["anchors"][state] = coverage["anchors"].get(state, 0) + 1
                coverage["locations"][location] = coverage["locations"].get(location, 0) + 1
                coverage["symbol_basis"][basis] = coverage["symbol_basis"].get(basis, 0) + 1
                keys = {"recording_id": event.get("recording_id"), "event_id": event.get("event_id"), "hunk": index}
                hunks.append({**keys, "session": recording.get("session"),
                              "independent_session": event.get("independent_session"), "harness": event.get("harness"),
                              "timestamp": event.get("timestamp"), "tool": event.get("tool"),
                              "repo_id": repository.repo_id if path else None, "path": path,
                              "location": location, "recorded_path": recorded_path, "change": change,
                              "source": source, "old_start": old_start, "old_lines": old_lines,
                              "new_start": new_start, "new_lines": new_lines, "added": added, "removed": removed,
                              "changed_start": min(recorded_changed), "changed_end": max(recorded_changed),
                              "anchor": state,
                              "current_start": min(live) if live else None,
                              "current_end": max(live) if live else None, "symbol_basis": basis,
                              "ref": event["ref"]})
                for item, count, innermost in _overlaps(items, lines_at):
                    symbols.append({**keys, "repo_id": repository.repo_id, "path": path, "basis": basis,
                                    "kind": item["kind"], "name": item["name"], "qualname": item["qualname"],
                                    "parent": item["parent"], "line": item["line"], "end_line": item["end_line"],
                                    "changed_lines": count, "innermost": innermost, "parse_state": parse_state})
    coverage["hunks"] = len(hunks)
    return hunks, symbols, coverage
