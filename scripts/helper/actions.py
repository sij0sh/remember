"""One function per agent action; entrypoints only parse and dispatch."""
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

from .discovery import SETTINGS, candidate_files, default_recordings, header, normalized_root
from . import cite, commit_links, edit_hunks, git_file_history, repo_files
from .bindings import parse_params, parse_types, types_for
from .context import changed_components, context_at_request
from .engine import EngineError, Input, evaluate
from .enrichment import Repository
from .envelope import LEVELS, clip
from .options import (CANDIDATES_DEFAULT, CANDIDATES_PROJECT, FILES_DEFAULT, FILES_PROJECT,
                      TEXT_BYTES_AUTO_CAP, TEXT_BYTES_DEFAULT,
                      DiscoveryOptions, ScanOptions, command_line)
from .projection import FIELDS, accepted, brief, clean, human, next_for, project_row
from .profiles import PROFILES, content_text, detected, emitted_kinds, first, pointer
from .records import RecordCache, new_scope, read_ref, records, safe_ref
from .reader import scan_file

SESSIONS = str(Path(__file__).resolve().parents[1] / "sessions.py")


def rerun(purpose, **flags):
    """This invocation again with the given flags replaced: a ready-to-run recovery command."""
    argv = list(sys.argv[1:]) or ["overview"]
    for flag, value in flags.items():
        name = "--" + flag.replace("_", "-")
        kept, skip = [], False
        for arg in argv:
            if skip:
                skip = False
            elif arg == name:
                skip = True
            elif not arg.startswith(name + "="):
                kept.append(arg)
        argv = kept + [name, str(value)]
    # coverage marks a command the byte budget keeps, so an incomplete scan always shows its rerun.
    return {"purpose": purpose, "argv": command_line([sys.executable, SESSIONS, *argv]), "coverage": True}


def _size(path):
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def switched(action, drop=(), **flags):
    """This invocation as another action: positional recordings and shared filters kept, named flags dropped."""
    argv = list(sys.argv[1:]) or ["overview"]
    argv[0] = action
    kept, skip = [], False
    for arg in argv:
        if skip:
            skip = False
        elif arg in drop:
            skip = True
        elif not any(arg.startswith(name + "=") for name in drop):
            kept.append(arg)
    for flag, value in flags.items():
        kept += ["--" + flag.replace("_", "-")] + ([] if value is True else [str(value)])
    return command_line([sys.executable, SESSIONS, *kept])


def all_requests(count):
    """The human requests the overview sampled from: a recovery command, kept under any budget."""
    # About 700 encoded bytes per previewed request row plus the envelope's coverage.
    # One page, not the whole list: a large archive would otherwise suggest a 100 KB+ response.
    page = min(count, 20)
    return {"purpose": f"List human requests, {page} per page; add --offset to page through {count}",
            "argv": switched("select", drop=("--text", "--path", "--limit", "--max-bytes", "--human", "--kind",
                                             "--role", "--preview-chars"),
                             human=True, fields="session,timestamp,turn,text,ref", preview_chars=200,
                             limit=page, max_bytes=max(16384, 8192 + 700 * page)),
            "coverage": True}


def text_budget(args, files, gscope):
    """Explicit --max-text-bytes, else the bytes this group will scan, within the default and the cap."""
    if args.max_text_bytes is not None:
        gscope["text_budget"] = args.max_text_bytes
        return args.max_text_bytes
    size = sum(_size(path) for path in files)
    budget = max(TEXT_BYTES_DEFAULT, min(size, args.max_scan_bytes, TEXT_BYTES_AUTO_CAP))
    gscope["text_budget"] = budget
    return budget


def coverage_next(groups, scope):
    """Commands that remove a reported scan, discovery or text limit; empty when none applies."""
    items = []
    if scope.get("discovery_complete") is False and scope.get("candidates_needed"):
        items.append(rerun("Read every discovery candidate", max_candidates=scope["candidates_needed"]))
    files = max((g["scope"].get("files_selected", 0) + g["scope"].get("files_omitted_limit", 0)
                 for g in groups if g["scope"].get("files_omitted_limit")), default=0)
    if files:
        items.append(rerun("Scan every selected recording", max_files=files))
    scan = max((g["scope"].get("bytes_selected", 0) for g in groups if g["scope"].get("scan_limit_reached")), default=0)
    if scan:
        items.append(rerun("Read every byte of the selected recordings", max_scan_bytes=scan))
    limited = [g["scope"] for g in groups if g["scope"].get("event_limit_reached")]
    if limited:
        # Events per byte read so far estimates the projection the remaining bytes need.
        events = max(max(2 * s.get("events_projected", 0),
                         s.get("events_projected", 0) * s.get("bytes_selected", 0) // max(1, s.get("bytes_scanned", 0)) + 1)
                     for s in limited)
        items.append(rerun("Project every event of the selected recordings", max_events=events))
    text = max((g["scope"].get("text_bytes_stored", 0) + g["scope"]["text_bytes_omitted"]
                for g in groups if g["scope"].get("text_bytes_omitted")), default=0)
    if text and scope.get("text_complete") is False:
        items.append(rerun("Store full text for every scanned event", max_text_bytes=text))
    for g in groups:
        for path in g["scope"].get("project_unknown_files", [])[:1]:
            items.append({"purpose": "Inspect a recording with no recorded root",
                          "argv": command_line([sys.executable, SESSIONS, "overview", "--project", "", path])})
    return items


def overview(groups, args):
    def request(e):
        return human(e) if args.human else e["kind"] == "user_message" and e.get("origin", "human") == "human"
    counts = Counter()
    outcomes = Counter()
    paths = Counter()
    commands = Counter()
    requests = []
    leads = []
    prompts = 0
    for g in groups:
        for e in g["events"]:
            counts[e["kind"]] += 1
            # Count one call/direct operation, never both call and result.
            if e["kind"] in ("tool_call", "command", "file_read", "file_edit"):
                outcomes[e.get("outcome", "unknown")] += 1
                if e.get("path"):
                    paths[(e.get("operation", e["kind"]), e["path"])] += 1
                if e.get("command"):
                    commands[e["command"]] += 1
            if request(e):
                requests.append(e)
            elif e["kind"] == "user_message" and e.get("origin", "human") == "human":
                prompts += 1
            if (e["kind"] in ("gap", "unknown") or e.get("state") == "unsupported"
                    or (e["kind"] != "tool_result" and e.get("outcome") == "failed")):
                leads.append(e)
    relevant = [e for e in requests if accepted(e, args)] if args.text or args.path else []
    # Breadth: relevant, latest, earliest, then representative recordings.
    chosen = []
    pool = relevant + (requests[-1:] + requests[:1]) + [next(e for e in g["events"] if request(e)) for g in groups if any(request(e) for e in g["events"])] + requests
    seen = set()
    for e in pool:
        key = (e["ref"]["file"], e["ref"]["line"], e["ref"]["item"])
        if key not in seen:
            seen.add(key)
            chosen.append(e)
        if len(chosen) == 4:
            break
    return {"requests": [brief(e, args) for e in chosen],
            "activity": {"events": dict(counts), "operation_outcomes": dict(outcomes),
                         "request_count": len(requests), "requests_shown": len(chosen),
                         **({"subagent_prompts_excluded": prompts} if prompts else {})},
            "files": [{"operation": op, "path": path, "occurrences": n} for (op, path), n in paths.most_common(5)],
            "commands": [{"command": cmd, "occurrences": n} for cmd, n in commands.most_common(5)],
            "leads": [brief(e, args) for e in leads[:5]],
            "omissions": {"requests_selection": max(0, len(requests) - len(chosen)), "leads_selection": max(0, len(leads) - 5)},
            "_next": next_for(chosen[0])[:1] + next_for(leads[0])[1:2] if chosen and leads else
                     next_for((chosen or leads)[0])[:1] if chosen or leads else []}


def context(groups, ref, args, scope):
    if args.mode == "request":
        events = groups[0]["events"]
        replay = context_at_request(events, ref)
        items = replay["items"]
        offset = max(0, len(items) - args.tail) if args.tail else args.offset or 0
        rows = [{**row, "cite": cite.token(row["source_ref"])} if row.get("source_ref") else row
                for row in items[offset:offset + (args.tail or args.limit)]]
        out = {"scope": scope, "harness": groups[0]["harness"], "guarantee": replay["guarantee"],
               "relationship_basis": replay.get("relationship_basis"),
               "request_ref": replay["request_ref"], "cite": cite.token(replay["request_ref"]), "matched": len(items),
               "results": rows, "context_items": rows, "dropped": replay["dropped"],
               "omissions": {"selection": max(0, len(items) - len(rows))},
               "next": ["Compare two request refs with --compare-ref to see changed components."]}
        if getattr(args, "compare_ref", None):
            other = context_at_request(events, safe_ref(args.compare_ref))
            out["compared_guarantee"] = other["guarantee"]
            out["changed"] = changed_components(replay, other)
        return out
    target = next((e for g in groups for e in g["events"] if e["ref"]["offset"] == ref["offset"] and e["ref"]["item"] == ref.get("item", 0)), None)
    if target is None:
        raise ValueError("Referenced event not found within scan bounds; raise scan/event limits")
    if target["ref"]["sha256"] != ref["sha256"]:
        raise ValueError("Source changed at this ref; search again")
    events = groups[0]["events"]
    if args.mode == "exchange":
        refs = [target["ref"], target.get("call_ref"), target.get("result_ref")]
        keys = {(r["offset"], r["item"]) for r in refs if r}
        found = [e for e in events if (e["ref"]["offset"], e["ref"]["item"]) in keys]
        basis = "exact_recorded_pairing; absent partner remains unknown"
    elif args.mode == "turn" and target.get("turn"):
        found = [e for e in events if e.get("turn") == target["turn"]]
        basis = target["turn_basis"]
    elif args.mode == "turn":
        found = [target]
        basis = "unresolved_turn; use explicit nearby mode for proximity"
    else:
        idx = events.index(target)
        found = events[max(0, idx - args.radius):idx + args.radius + 1]
        basis = "proximity_only; may include other branches"
    position = next(i for i, e in enumerate(found) if e is target)
    size = args.tail or args.limit
    if args.tail:
        # The end of a long turn: its closing claim, final results and last edits.
        start = max(0, len(found) - args.tail)
    elif args.offset is None:
        # Centre the page on the selected event, so a late event in a long turn is never off-page.
        start = max(0, min(position - args.limit // 2, len(found) - args.limit))
    else:
        start = args.offset
    rows = found[start:start + size]
    on_page = start <= position < start + len(rows)
    out = {"scope": scope, "harness": groups[0]["harness"], "guarantee": "transcript_only",
           "relationship_basis": basis, "matched": len(found), "cite": cite.token(target["ref"]),
           "selected": {"position": position, "on_page": on_page},
           "page": {"start": start, "end": start + len(rows)},
           # The selected event is what the caller asked to read: full text; neighbors are previews.
           "results": [{**clean(e, None), "selected": True, "cite": cite.token(e["ref"])} if e is target
                       else {**clean(e, args), "cite": cite.token(e["ref"])} for e in rows],
           "omissions": {"selection": max(0, len(found) - len(rows))},
           "next": next_for(target)}
    if start + len(rows) < len(found) and not args.tail:
        out["next"].append({"purpose": "Read the end of this turn",
                            "argv": switched("context", drop=("--offset", "--tail"), tail=args.limit)})
    if not on_page and not args.tail:
        out["next"].insert(0, rerun("Page that holds the selected event",
                                    offset=max(0, position - args.limit // 2)))
    if start > 0 and found[0]["kind"] == "user_message":
        out["turn_request_ref"] = found[0]["ref"]
    return out


def payload(args):
    ref = safe_ref(args.ref)
    record = read_ref(ref, args.max_record_bytes)
    ptr = args.pointer if args.pointer is not None else ref.get("pointer", "")
    missing = object()
    value = pointer(record, ptr, missing)
    if value is missing:
        raise ValueError("Pointer is unavailable in this record")
    text = value if isinstance(value, str) else content_text(value) if isinstance(value, list) else json.dumps(value, ensure_ascii=True)
    if isinstance(value, list) and not text:
        text = json.dumps(value, ensure_ascii=True)
    if args.find:
        position = text.casefold().find(args.find.casefold())
        start = max(0, position - args.chars // 3) if position >= 0 else args.start
    else:
        position = None
        start = max(0, len(text) - args.chars) if args.tail else args.start
    return {"scope": {"file": ref["file"], "source_checked": True}, "results": [{
        "ref": ref, "cite": cite.token(ref), "pointer": ptr, "text": text[start:start + args.chars],
        "text_start": start, "text_chars": len(text), "match_at": position,
        "text_omitted": max(0, len(text) - len(text[start:start + args.chars]))}]}


def raw(args):
    ref = safe_ref(args.ref)
    # Verify source with bounded memory, including an oversized record.
    h = hashlib.sha256()
    with open(ref["file"], "rb") as f:
        f.seek(ref["offset"])
        remaining = ref["length"]
        while remaining:
            chunk = f.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            h.update(chunk)
        if remaining or h.hexdigest() != ref["sha256"]:
            raise ValueError("Source changed at this ref; search again")
        start = max(0, ref["length"] - args.chars) if args.tail else args.start
        match_at = None
        if args.find:
            needle = args.find.encode()
            f.seek(ref["offset"])
            consumed, previous = 0, b""
            while consumed < ref["length"]:
                chunk = f.read(min(65536, ref["length"] - consumed))
                joined = previous + chunk
                hit = joined.find(needle)
                if hit >= 0:
                    match_at = consumed - len(previous) + hit
                    start = max(0, match_at - args.chars // 3)
                    break
                consumed += len(chunk)
                previous = joined[-max(0, len(needle) - 1):] if len(needle) > 1 else b""
        start = min(start, ref["length"])
        f.seek(ref["offset"] + start)
        text = f.read(min(args.chars, ref["length"] - start)).decode("utf-8", errors="replace")
    return {"scope": {"source_checked": True}, "results": [{"ref": ref, "cite": cite.token(ref), "text": text,
            "byte_start_in_record": start, "match_at": match_at,
            "note": "Raw encoded JSON bytes, not decoded field content"}]}


def _snapshot(path, scope, before):
    try:
        from pathlib import Path
        stat = Path(path).stat()
        scanned = scope.get("bytes_scanned", 0) - before
        scope.setdefault("snapshot", []).append({
            "file": str(Path(path).absolute()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "inode": stat.st_ino, "bytes_scanned": scanned, "scan_complete": scanned >= stat.st_size})
    except OSError:
        pass


def edges(groups, scope):
    """Exact recorded relationships; absent partners produce no edge."""
    rows = []
    for g in groups:
        events = [e for r in g["events"] for e in r["events"]]
        by_mid = {}
        for e in events:
            mid = e.get("external_message_id")
            if mid:
                by_mid.setdefault(mid, []).append(e)
        for e in events:
            if e["kind"] == "tool_call" and e.get("result_ref"):
                rows.append({"from_ref": e["ref"], "to_ref": e["result_ref"], "relation": "call_result",
                             "basis": "exact_recorded_call_id", "ref": e["ref"]})
            parent = e.get("external_parent_id")
            if parent and parent in by_mid:
                for source in by_mid[parent]:
                    rows.append({"from_ref": source["ref"], "to_ref": e["ref"], "relation": "recorded_parent",
                                 "basis": "recorded_parent_id", "ref": e["ref"]})
        for mid, same in by_mid.items():
            if len(same) > 1:
                for other in same[1:]:
                    rows.append({"from_ref": same[0]["ref"], "to_ref": other["ref"], "relation": "representation",
                                 "basis": "same_recorded_message_id; content differs", "ref": other["ref"]})
        turns = {}
        for e in events:
            if e.get("turn"):
                turns.setdefault(e["turn"], []).append(e)
        for turn, same in turns.items():
            anchor = next((e for e in same if e["kind"] == "user_message"), same[0])
            for other in same:
                if other is not anchor:
                    rows.append({"from_ref": anchor["ref"], "to_ref": other["ref"], "relation": "turn",
                                 "basis": anchor.get("turn_basis", "unknown"), "ref": other["ref"]})
    for row in rows:
        row["from_cite"], row["to_cite"] = cite.token(row["from_ref"]), cite.token(row["to_ref"])
    return {"scope": scope, "matched": len(rows), "grain": "one exact recorded edge per row",
            "results": rows, "omissions": {},
            "next": ["Expand either ref with context/payload; edges do not imply causality."]}


def describe():
    recordings = default_recordings()
    return {"results": [{"fields": list(FIELDS),
        "grain": "one normalized occurrence; tool_call and tool_result are separate rows",
        "schema": "every harness emits the same fields; a field a harness did not record is absent",
        "grouping": "search/select rows are one top-level results[], each tagged with harness and a cite token; "
                    "harnesses[] keeps per-harness scope, omissions and leads; overview requests stay per harness",
        "overview": ["compare harness groups, not raw counts across harness guarantees",
                     "requests are sampled user messages, not a task inventory; rows keep orientation fields only",
                     "ignored summaries/system/thinking and shell side effects are not reconstructed",
                     "activity covers scanned recordings; --text/--path rank request examples, not activity counts",
                     "operation_outcomes count one tool call or direct recorded operation; outcomes describe recordings"],
        "budget": {"levels": [name for name, _ in LEVELS],
                   "rule": "the first level whose JSON fits --max-bytes is emitted; detail.level names it and "
                           "detail.more_bytes is the exact budget for the previous level; omissions count what was shed"},
        "joins": ["ref identifies a file/record/item; call_ref/result_ref identify exact exchanges",
                  "parent_session marks a recorded subagent or fork; scope.sessions_independent counts it once",
                  "independent_session is the session a subagent or fork was recorded from, else session: "
                  "count(DISTINCT independent_session) equals scope.sessions_independent; "
                  "recording_id and session overcount subagents",
                  "session + turn groups recorded or labelled inferred context", "path activity excludes shell effects"],
        "text_states": ["full", "previewed", "budget_omitted", "absent", "redacted", "encrypted", "unsupported"],
        "states": ["present", "redacted", "encrypted", "unsupported", "not_recorded"],
        "event_kinds": ["user_message", "assistant_message", "tool_call", "tool_result", "command", "file_read",
                        "file_edit", "summary", "instruction", "reasoning", "lifecycle", "config", "capability",
                        "usage", "context_change", "attachment", "boundary", "gap", "unknown"],
        "actions": ["overview", "projects", "search", "select", "duplicates", "recurrence", "records", "edges", "context",
                    "payload", "raw", "cite", "describe"],
        "recurrence": "failed results grouped by --text term, error signature or command; counts independent sessions "
                      "and gives the first failed result's ref in each; mentions outside failed results are not counted; "
                      "tool and environment failures are hidden unless --include-noise or --text",
        "citations": ["context, edges, payload and raw rows carry cite tokens (id@line[.item]#hash); --ref accepts one",
                      "search, select and overview rows are locators and carry none: expand before citing",
                      "cite REPORT checks every token, full commit SHA, bare hex prefix and bare session id in a report"],
        "relations": {"events": "normalized occurrences from profiles; one ref per occurrence",
                      "records": "raw decoded records, including records a profile ignored or marked unsupported"},
        "profiles": {h: {"profile": p["name"] + "-v" + p["schema_version"], "schema_version": p["schema_version"],
                         "version": p.get("version", p["schema_version"]),
                         "event_kinds": sorted(emitted_kinds(p.get("records", []))),
                         "guarantees": p.get("guarantees", {})} for h, p in PROFILES.items()},
        "defaults": {"action": "overview", "settings": str(SETTINGS),
                     "recordings": {h: [str(x) for x in paths] for h, paths in recordings.items()},
                     "project": "invocation directory; '' disables"},
        "projection": "--fields selects named event fields; --field alias=/pointer extracts from original record",
        "analytics": "select --sql 'SELECT ...' queries every matched row (view events, aliased rows, with a harness column); "
                     "rows.py joins piped envelopes or --view NAME=COMMAND producers in memory and marks clipped input as partial",
        "repository": {
            "usage": "select --include git,files[,symbols,content,edits] [--repo PATH] adds views beside events",
            "tables": {"commit_links": {"grain": commit_links.GRAIN, "columns": commit_links.COLUMNS},
                       "commits": {"grain": git_file_history.COMMIT_GRAIN, "columns": git_file_history.COMMIT_COLUMNS},
                       "changes": {"grain": git_file_history.CHANGE_GRAIN, "columns": git_file_history.CHANGE_COLUMNS},
                       "files": {"grain": repo_files.FILE_GRAIN, "columns": repo_files.FILE_COLUMNS},
                       "code_items": {"grain": repo_files.ITEM_GRAIN, "columns": repo_files.ITEM_COLUMNS},
                       "edit_hunks": {"grain": edit_hunks.HUNK_GRAIN, "columns": edit_hunks.HUNK_COLUMNS},
                       "edit_symbols": {"grain": edit_hunks.SYMBOL_GRAIN, "columns": edit_hunks.SYMBOL_COLUMNS}},
            "joins": ["events -> commit_links USING (recording_id, event_id)",
                      "commit_links -> commits/changes USING (repo_id, sha)",
                      "changes.new_path or events.repo_path -> files.path (same repo_id)"],
            "event_objects": "git[]: witnesses with their commit or null; file: current-file metadata or null; "
                             "one row per event either way",
            "guarantees": ["commit_links keeps every witness, including unresolved (sha null) and failed commands",
                           "confidence recorded comes from command output; inferred is a unique subject match "
                           "within a day of the command; ambiguous_subject stays unresolved",
                           "changes compare against parent_sha; merges use their first parent",
                           "a path match shows association, not that today's content matches history",
                           "symbols parse Python (ast) and Rust, Go, TypeScript/JavaScript and shell (tree-sitter); "
                           "other files are parse_state unsupported, and a missing grammar is parser_unavailable",
                           "import module and qualname stay literal source text; target_path/target_state map them by stated rules",
                           "edit_symbols basis: at_edit (recorded post-edit file) or current (hunk text unique today)"]}}]}


def lineages(recordings):
    """Each recording's independent session: a subagent or fork counts with the session it was recorded from."""
    by_file = {r["file"]: r for r in recordings}
    def key(r, depth=0):
        parent = r.get("parent_session")
        if not parent:
            return r["session"]
        # Pi records the parent as a file path; resolve it to that recording's own lineage.
        found = by_file.get(parent)
        return key(found, depth + 1) if found is not None and found is not r and depth < 32 else parent
    return {r["file"]: key(r) for r in recordings}


def combined_scope(scope, groups):
    """Envelope coverage: discovery facts plus the sum of every harness's counters."""
    for g in groups:
        for key, value in g["scope"].items():
            if isinstance(value, bool):
                if key in ("scan_complete", "discovery_complete", "text_complete"):
                    scope[key] = scope.get(key, True) and value
                elif value:
                    scope[key] = True
            elif isinstance(value, int):
                scope[key] = scope.get(key, 0) + value
            elif isinstance(value, list) and key == "snapshot":
                scope.setdefault(key, []).extend(value)
    scope.setdefault("scan_complete", True)
    scope.setdefault("text_complete", True)
    scope["harnesses"] = [g["harness"] for g in groups]
    return scope


TOOL_KINDS = ("tool_call", "tool_result")


def rank(e):
    """Human requests, then assistant decisions and reasoning, then tool and system activity."""
    return 0 if human(e) else 1 if e["kind"] in ("assistant_message", "reasoning") else 2


def search(recordings, args, repository=None):
    matches = [e for g in recordings for e in g["events"] if accepted(e, args)]
    # Tool payloads echo files and logs: they bury the requests and decisions that explain them.
    tools = args.action == "search" and not (args.kind or args.role == "tool" or args.include_tools)
    hidden = {k: sum(e["kind"] == k for e in matches) for k in TOOL_KINDS} if tools else {}
    hidden = {k: n for k, n in hidden.items() if n}
    if hidden:
        matches = [e for e in matches if e["kind"] not in hidden]
    if getattr(args, "order", "time") == "rank":
        matches.sort(key=rank)
    rows = matches[args.offset:args.offset + args.limit]
    cache = RecordCache(args.max_record_bytes)
    if getattr(args, "fields", None):
        out = [project_row(e, args, cache=cache) for e in rows]
    else:
        out = [clean(e, args) for e in rows]
    if repository is not None:
        out = [repository.enrich(e, row) for e, row in zip(rows, out)]
    # Every row is citable and expandable as is; _order interleaves harness groups by rank tier.
    ranked = getattr(args, "order", "time") == "rank"
    out = [{**cite.tagged(row, e["ref"]), "_order": rank(e) if ranked else 0} for e, row in zip(rows, out)]
    gaps = [e for g in recordings for e in g["events"] if e["kind"] in ("gap", "unknown")]
    return {"matched": len(matches), "results": out,
            **({"excluded": {**hidden, "include_with": "--include-tools"}} if hidden else {}),
            "leads": [cite.tagged(clean(e, args), e["ref"]) for e in gaps[:3]],
            "omissions": {"selection": max(0, len(matches) - len(rows))},
            "next": sum((next_for(e)[:1] for e in rows[:2]), []) + sum((next_for(e)[-1:] for e in gaps[:1]), [])}


def _hydrate(event, cache):
    """Re-read one budget-omitted event when --full-text is requested."""
    if event.get("text_state") != "budget_omitted":
        return event
    try:
        record = cache.get(event["ref"])
        ptr = event.get("payload_pointer") or event["ref"].get("pointer", "")
        value = pointer(record, ptr)
        if value is None:
            return event
        text = value if isinstance(value, str) else content_text(value) if isinstance(value, list) else json.dumps(value, ensure_ascii=True)
        if not text:
            return event
        return {**event, "text": text, "text_chars": len(text), "text_start": 0,
                "text_omitted": 0, "text_state": "full"}
    except (OSError, ValueError, TypeError, KeyError):
        return event


# Columns and views that exist only when select --include adds them.
INCLUDED = {"column": {"repo_path": "files", "file": "files", "git": "git"},
            "table": {"commit_links": "git", "commits": "git", "changes": "git", "files": "files",
                      "code_items": "symbols", "edit_hunks": "edits", "edit_symbols": "edits"}}


def include_hint(message, include):
    """Name the missing --include when a query references a view or column it adds."""
    found = re.search(r'Referenced column "(\w+)" not found', message) or re.search(r"Table with name (\w+) does not exist", message)
    if not found:
        return ""
    name = found.group(1)
    needed = INCLUDED["column" if found.re.pattern.startswith("Referenced") else "table"].get(name)
    have = {part.strip() for part in (include or "").split(",") if part.strip()}
    if "edits" in have:
        have.add("symbols")
    if "symbols" in have:
        have.add("files")
    if not needed or needed in have:
        return ""
    return f"; {name} exists only with --include {','.join(sorted(have | {needed}))}"


# Include names an agent tends to query as if they were views.
VIEW_ALIASES = {"git": "commits", "edits": "edit_hunks", "hunks": "edit_hunks", "symbols": "code_items",
                "content": "files", "event": "events", "commit": "commits", "file": "files"}


def table_hint(message):
    """Name the view a misspelled or include-named table most likely meant."""
    found = re.search(r"Table with name (\w+) does not exist", message)
    if not found:
        return ""
    import difflib
    name = found.group(1).lower()
    views = ["events", "rows", *INCLUDED["table"]]
    guess = VIEW_ALIASES.get(name) or next(iter(difflib.get_close_matches(name, views, n=1)), None)
    return f"; did you mean {guess}? Views: {', '.join(views)} (sessions.py describe lists columns)" if guess else ""


# Views derived from scanned recordings; every other view comes from the repository or none at all.
EVENT_VIEWS = ("rows", "events", "commit_links", "edit_hunks", "edit_symbols")


def repository_only(sql):
    """True when a query reads no event-derived view, so recordings need no scan.

    Unknown table names also take this path: the engine reports the bad view (and
    names the likely one) even when no recording directory exists. Event views
    still need the scan, whether or not the query also names an unknown table.
    """
    try:
        import duckdb
        # Named parameters ($name) make the statement unpreparable for name extraction; NULL parses alike.
        tables = {name.lower() for name in duckdb.get_table_names(re.sub(r"\$\w+", "NULL", sql))}
    except Exception:  # missing engine or a parser limitation (such as JOIN ... USING): decide by name
        if re.search(r"\b(" + "|".join(EVENT_VIEWS) + r")\b", sql, re.IGNORECASE):
            return False
        tables = {name.lower() for name in re.findall(r"\b(?:from|join)\s+\"?(\w+)", sql, re.IGNORECASE)}
    return not (tables & set(EVENT_VIEWS))


def sql_query(groups, scope, args, repository=None):
    """Query every matched row in-process: no paging or byte budget before aggregation."""
    if args.offset:
        raise ValueError("--offset pages search rows; with --sql use LIMIT/OFFSET in the query")
    matched = {g["harness"]: [e for r in g["events"] for e in r["events"] if accepted(e, args)] for g in groups}
    total = sum(map(len, matched.values()))
    if total > args.max_query_rows:
        raise ValueError(f"{total} matched rows exceed --max-query-rows; narrow filters or raise the limit")
    # Complete projections feed the engine; display previews do not clip aggregates.
    cache = RecordCache(args.max_record_bytes)
    full = bool(getattr(args, "full_text", False))
    types = parse_types(getattr(args, "type", []), "rows")
    params = parse_params(getattr(args, "param", []))
    events = [(_hydrate(e, cache) if full else e) for found in matched.values() for e in found]
    # Text coverage of the query is the matched rows' text, after any --full-text re-read.
    omitted = sum(e.get("text_state") == "budget_omitted" for e in events)
    if full:
        scope["text_rehydrated_events"] = sum(e.get("text_state") == "budget_omitted" for found in matched.values()
                                              for e in found) - omitted
    scope["text_scan_truncated_events"] = scope.get("text_truncated_events", 0)
    scope.update(text_truncated_events=omitted, text_complete=not omitted)
    rows = [project_row(e, args, clean(e, complete=True), complete=True, cache=cache) for e in events]
    # Declared schema keeps an empty selection queryable instead of unbound.
    schema = [name for name in list(FIELDS) + args.fields.split(",")
              + [definition.partition("=")[0] for definition in args.field] if name]
    upstream = {"text_truncated_events": omitted}
    reasons = [reason for key, reason in (("scan_complete", "scan_limit"), ("discovery_complete", "discovery_limit"))
               if scope.get(key) is False]
    if scope.get("project_unknown"):
        # Recordings without a recorded root might belong to the project; they were not read.
        reasons.append("project_unknown")
    if reasons:
        upstream.update(complete=False, incomplete_reasons=reasons)
    input_types = types_for(types, "events", "t0", True)
    extra = []
    if repository is not None:
        rows = [repository.enrich(e, row) for e, row in zip(events, rows)]
        added = repository.event_columns()
        schema += list(added)
        input_types = {**added, **input_types}
        extra = repository.inputs(lambda name, alias: types_for(types, name, alias))
        scope["repository"] = repository.coverage
    try:
        result = evaluate([Input("events", rows, upstream, schema, input_types, "one matched event"), *extra],
                          args.sql, args.limit, args.query_seconds, args.memory_limit,
                          params=params, grain="one matched event")
    except EngineError as error:
        raise EngineError(str(error) + include_hint(str(error), args.include) + table_hint(str(error))) from None
    # An unread events view is empty; its column list would only spend the byte budget.
    for summary in result["scope"]["inputs"][0 if scope.get("recordings_read") is False else 1:]:
        # Repository views have fixed schemas (sessions.py describe); keep coverage, not column lists.
        summary.pop("columns", None)
        summary.pop("types", None)
    scope.update(result["scope"])
    scope["matched_by_harness"] = {h: len(found) for h, found in matched.items()}
    scope["full_text"] = full
    # matched counts query output rows; events_matched counts the input rows the query read.
    returned = result["omissions"].get("query_rows_total", len(result["results"]))
    out = {"scope": scope, "matched": returned, "events_matched": total, "query": args.sql,
           "counting_scope": "matched: query output rows; events_matched: every matched input row in the scanned "
                             "selection; scope.scan_complete bounds that selection",
           "results": result["results"], "omissions": result["omissions"],
           "next": coverage_next(groups, scope)
           + ["Select contributing rows with the same filters (no --sql) to inspect evidence refs."]}
    if result.get("status"):
        out["status"] = result["status"]
    return out


# Rows that hold a failed operation's output; a tool_call only carries its result's outcome.
FAILURE_KINDS = ("tool_result", "command", "file_read", "file_edit")
ERROR_LINE = re.compile(r"error|fail|exception|denied|not found|no such|invalid|cannot|refused|timed? ?out|panic|fatal",
                        re.IGNORECASE)
# Failures of the agent's tooling or environment, not the project: missing paths, wrong cwd, absent commands.
NOISE_KINDS = ("file_read", "file_edit")
NOISE_LINE = re.compile(r"no such file or directory|enoent|eisdir|is a directory|not a directory|command not found",
                        re.IGNORECASE)
# A harness's exit-status banner says a command failed, not how; the output after it names the failure.
EXIT_BANNER = re.compile(r"(?:exit code|exit status|process exited with code)\s*-?\d+", re.IGNORECASE)


def failure_signature(text):
    """The first error-like output line, with paths, hashes and numbers normalized away."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    lines = [line for line in lines if not EXIT_BANNER.fullmatch(line)] or lines
    line = next((line for line in lines if ERROR_LINE.search(line)), lines[0] if lines else "")
    line = re.sub(r"(?:~|\.{0,2})?(?:/[\w.@+-]+)+/?", "<path>", line)
    line = re.sub(r"\b[0-9a-f]{7,}\b", "<hex>", line)
    line = re.sub(r"\d+", "N", line)
    return re.sub(r"\s+", " ", line)[:160] or "(no output)"


def command_signature(e):
    command = re.sub(r"\s+", " ", str(e.get("command") or "").strip())
    if command:
        return command[:160]
    return " ".join(str(e[k]) for k in ("tool", "path") if e.get(k)) or e["kind"]


def recurrence(groups, args, scope):
    """Failed results grouped by error, command or term, counted by independent session."""
    cache = RecordCache(args.max_record_bytes)
    failed = [_hydrate(e, cache) for g in groups for r in g["events"] for e in r["events"]
              if e["kind"] in FAILURE_KINDS and e.get("outcome") == "failed" and accepted(e, args)]
    # Counts are over full output text; only an unreadable record keeps a preview.
    omitted = sum(e.get("text_state") == "budget_omitted" for e in failed)
    scope.update(text_truncated_events=omitted, text_complete=not omitted)
    noise = 0
    if not args.terms and not args.include_noise:
        kept = [e for e in failed if e["kind"] not in NOISE_KINDS
                and not NOISE_LINE.search(failure_signature(e.get("text")))]
        noise, failed = len(failed) - len(kept), kept
    buckets = {}
    if args.terms:
        # One group per term, zero included: a row mentioning two terms counts under both.
        for term in args.terms:
            buckets[term] = [e for e in failed
                             if term.casefold() in (str(e.get("text", "")) + "\n" + str(e.get("command", ""))).casefold()]
    else:
        key = command_signature if args.by == "command" else lambda e: failure_signature(e.get("text"))
        for e in failed:
            buckets.setdefault(key(e), []).append(e)
    rows = []
    for name, found in buckets.items():
        lineage = {}
        for e in found:
            lineage.setdefault(e["independent_session"], []).append(e)
        if not args.terms and len(lineage) < args.min_sessions:
            continue
        stamps = sorted(str(e["timestamp"]) for e in found if e.get("timestamp"))
        examples = []
        for session, same in sorted(lineage.items(), key=lambda item: str(item[1][0].get("timestamp") or "")):
            first = same[0]
            example = {"independent_session": session, "failures": len(same), "timestamp": first.get("timestamp"),
                       "ref": first["ref"]}
            if args.terms or args.by == "error":
                example["command"] = command_signature(first)[:120]
            if args.terms or args.by == "command":
                text = str(first.get("text", ""))
                at = text.casefold().find(name.casefold()) if args.terms else -1
                example["text"] = clip(text, 120, match_at=at if at >= 0 else None)["text"]
            examples.append(example)
        rows.append({"key": name, "sessions_independent": len(lineage),
                     "recordings": len({e["recording_id"] for e in found}), "failures": len(found),
                     **({"first_seen": stamps[0], "last_seen": stamps[-1]} if stamps else {}),
                     "examples": examples[:args.examples],
                     **({"examples_omitted": len(examples) - args.examples} if len(examples) > args.examples else {})})
    if not args.terms:
        rows.sort(key=lambda r: (-r["sessions_independent"], -r["failures"], r["key"]))
    page = rows[args.offset:args.offset + args.limit]
    out = {"scope": scope, "matched": len(failed), "grain": "one failure group per row; examples hold the first failed "
                                                            "result of each independent session",
           "grouped_by": "term" if args.terms else args.by,
           "counting_scope": "failed results only (not mentions in requests, replies or file reads); "
                             "sessions_independent counts a subagent or fork with its parent",
           "results": page,
           "omissions": {"selection": max(0, len(rows) - len(page)),
                         **({"below_min_sessions": len(buckets) - len(rows)} if not args.terms else {}),
                         **({"environment_noise": noise} if noise else {})},
           "next": coverage_next(groups, scope)
           + ["Expand each example with context --ref REF --mode exchange before calling a failure recurring; "
              "cite the tokens that expansion returns."]}
    if any(r.get("examples_omitted") for r in page):
        out["next"].append(rerun("Show one example per independent session",
                                 examples=max(r["sessions_independent"] for r in page)))
    below = out["omissions"].get("below_min_sessions", 0)
    if below and not rows:
        # Every group was seen in fewer independent sessions than --min-sessions; say so instead of looking empty.
        out["notes"] = [f"{below} failure groups were each seen in fewer than {args.min_sessions} independent sessions."]
        out["next"].append(rerun("Show failure groups seen in a single session", min_sessions=1))
    return out


def _native_type(record):
    value = record.get("type")
    if value is None and isinstance(record.get("payload"), dict):
        value = record["payload"].get("type")
    return str(value or "unknown")


def _record_text(record):
    """Best text surface and its pointer, without interpreting the record as an event."""
    candidates = [("/message/content", record.get("message", {}).get("content") if isinstance(record.get("message"), dict) else None),
                  ("/payload/message", record.get("payload", {}).get("message") if isinstance(record.get("payload"), dict) else None),
                  ("/payload/item/content", record.get("payload", {}).get("item", {}).get("content") if isinstance(record.get("payload"), dict) and isinstance(record["payload"].get("item"), dict) else None),
                  ("/text", record.get("text")), ("", record)]
    for ptr, value in candidates:
        if isinstance(value, str) and value:
            return value, ptr
        if isinstance(value, list):
            text = content_text(value)
            if text:
                return text, ptr
    return json.dumps(record, ensure_ascii=True), ""


def scan_records(groups, args, scope):
    """Raw-record relation: every decoded record, including profile-ignored ones."""
    found = []
    total = 0
    for g in groups:
        for path in g["files"]:
            before = g["scope"]["bytes_scanned"]
            file_harness = args.harness if args.harness != "auto" else header(path, args.max_record_bytes, 262144)[0]
            for ref, record, error in records(path, g["scope"], args.max_record_bytes, args.max_scan_bytes):
                harness = file_harness or (detected(record) if record else None)
                if error:
                    if args.parse_status and args.parse_status != error:
                        continue
                    if args.text:
                        continue
                    total += 1
                    found.append({"session": str(path), "harness": harness or "unknown", "kind": "record",
                                  "line": ref["line"], "parse_status": error, "item_count": 1,
                                  "native_type": "unknown", "native_subtype": None,
                                  "text": "", "text_chars": 0, "text_start": 0, "text_omitted": 0,
                                  "text_state": "unsupported", "match_at": None,
                                  "payload_pointer": "", "ref": ref})
                    continue
                native = _native_type(record)
                subtype = record.get("subtype") or (record.get("payload") or {}).get("subtype") if isinstance(record.get("payload"), dict) else record.get("subtype")
                text, ptr = _record_text(record)
                searchable = json.dumps(record, ensure_ascii=True)
                if args.text and not any(t.casefold() in searchable.casefold() for t in args.text):
                    continue
                requested = getattr(args, "native_type", []) or args.kind
                if requested and native not in requested:
                    continue
                if args.parse_status and args.parse_status != "ok":
                    continue
                total += 1
                position = None
                if args.text:
                    for t in args.text:
                        position = text.casefold().find(t.casefold())
                        if position >= 0:
                            break
                    if position is None:
                        position = max(0, searchable.casefold().find(t.casefold()))
                profile = PROFILES.get(harness) if harness else None
                session = first(profile["session"].get("external_id", []), record) if profile else None
                timestamp = first(profile.get("timestamp", []), record) if profile else None
                shown = clip(text, args.preview_chars, match_at=position if position is not None else None)
                found.append({"session": session or str(path), "harness": harness or "unknown",
                              "kind": "record", "line": ref["line"], "parse_status": "ok", "item_count": 1,
                              "native_type": native, "native_subtype": subtype, "timestamp": timestamp,
                              "text": shown["text"],
                              "text_chars": shown["text_chars"], "text_start": shown["text_start"],
                              "text_omitted": shown["text_omitted"],
                              "text_state": "previewed" if shown["text_omitted"] else "full",
                              "match_at": position, "payload_pointer": ptr, "ref": ref})
            _snapshot(path, g["scope"], before)
    rows = found[args.offset:args.offset + args.limit]
    scope["snapshot"] = [item for g in groups for item in g["scope"].get("snapshot", [])]
    scope["snapshot_basis"] = "refs are append-only; snapshot is the invocation-time view"
    return {"scope": scope, "matched": total, "grain": "one raw recording per row",
            "results": rows, "omissions": {"selection": max(0, len(found) - len(rows))},
            "next": ["Use payload with a row ref to decode the complete record."]}


def projects(args):
    """Recorded project roots from bounded header reads only; no recording is parsed."""
    from datetime import datetime, timezone
    scope = {"duplicates_skipped": 0}
    roots = {}
    candidate_files(DiscoveryOptions.from_args(args), scope, roots)
    found = [{"harness": harness, "root": root, "recordings": n,
              "newest": datetime.fromtimestamp(mtime / 1e9, timezone.utc).isoformat(timespec="seconds")}
             for (harness, root), (n, mtime) in roots.items()]
    found.sort(key=lambda r: r["newest"], reverse=True)
    rows = found[args.offset:args.offset + args.limit]
    here = normalized_root(os.getcwd())
    def covers(root):
        root = normalized_root(root).rstrip("\\/")
        return here == root or here.startswith(root + ("\\" if "\\" in root else "/"))
    current = [r for r in found if r["root"] and covers(r["root"])]
    # The invocation directory's own roots stay visible however far down the recency order they fall.
    rows += [{**r, "listed_because": "invocation_directory"} for r in current if r not in rows]
    scope["project_unknown"] = sum(r["recordings"] for r in found if r["root"] is None)
    next_items = coverage_next([], scope) + [
        {"purpose": "Overview of this recorded root",
         "argv": command_line([sys.executable, SESSIONS, "overview", "--project", r["root"]])}
        for r in rows if r["root"]][:1]
    return {"scope": scope, "matched": len(found), "grain": "one recorded root per harness",
            "results": rows, "omissions": {"selection": max(0, len(found) - len(rows))}, "next": next_items}


# Repeated windows of normalized code lines, merged into the longest aligned region per pair of
# locations. Blank, bracket-only and comment-led lines are skipped, so line ranges are source lines.
DUPLICATES_SQL = r"""
WITH source AS (
  SELECT path, i AS line, regexp_replace(trim(split_part(content, chr(10), i::INTEGER)), '\s+', ' ', 'g') AS text
  FROM files, range(1, lines + 1) t(i)
  WHERE content IS NOT NULL AND language IN ('python', 'javascript', 'typescript', 'go', 'rust', 'java',
        'kotlin', 'c', 'cpp', 'csharp', 'ruby', 'php', 'swift', 'shell', 'lua')),
kept AS (
  SELECT path, line, text, row_number() OVER (PARTITION BY path ORDER BY line) AS n FROM source
  WHERE text <> '' AND NOT regexp_full_match(text, '[{}()\[\];,]+') AND NOT regexp_matches(text, '^(//|#|\*|/\*)')),
spans AS (
  SELECT path, n, min(line) OVER w AS start_line, max(line) OVER w AS end_line, count(*) OVER w AS size,
         string_agg(text, chr(10)) OVER w AS body
  FROM kept WINDOW w AS (PARTITION BY path ORDER BY n ROWS BETWEEN CURRENT ROW AND CAST($span AS INTEGER) - 1 FOLLOWING)),
repeated AS (
  SELECT * FROM spans WHERE size = CAST($span AS INTEGER) AND length(body) >= CAST($min_chars AS INTEGER)),
pairs AS (
  SELECT a.path AS path_a, a.n AS n_a, a.start_line AS start_a, a.end_line AS end_a,
         b.path AS path_b, b.n - a.n AS shift, b.start_line AS start_b, b.end_line AS end_b
  FROM repeated a JOIN repeated b ON a.body = b.body
   AND (a.path < b.path OR (a.path = b.path AND a.n + CAST($span AS INTEGER) <= b.n))),
runs AS (
  SELECT *, n_a - row_number() OVER (PARTITION BY path_a, path_b, shift ORDER BY n_a) AS run FROM pairs)
SELECT path_a || ':' || min(start_a) || '-' || max(end_a) AS first,
       path_b || ':' || min(start_b) || '-' || max(end_b) AS second,
       count(*) + CAST($span AS INTEGER) - 1 AS code_lines
FROM runs GROUP BY path_a, path_b, shift, run
ORDER BY code_lines DESC, first, second
"""


def duplicates_query(args):
    """duplicates is select --include content with the shipped duplicate-window query."""
    if args.sql:
        raise ValueError("duplicates runs its own query; use select --sql for another")
    if args.span < 2 or args.min_chars < 1:
        raise ValueError("--span must be at least 2 and --min-chars positive")
    have = {part.strip() for part in (args.include or "").split(",") if part.strip()}
    args.include = ",".join(sorted(have | {"content"}))
    args.param = [*args.param, f"span={args.span}", f"min_chars={args.min_chars}"]
    args.sql = DUPLICATES_SQL
    args.action = "select"
    args.builtin = f"duplicates span={args.span} min_chars={args.min_chars}"


def lint_citations(args):
    """Check every citation in written reports: cite tokens resolve, commits are full and exist."""
    repo = os.path.abspath(args.repo) if args.repo else os.getcwd()
    rows, problems = cite.lint(args.files, repo, args.max_record_bytes)
    # Problems lead, so a budget cut sheds passing citations first.
    shown = problems + [r for r in rows if r["state"] == "ok"]
    return {"scope": {"reports": list(args.files), "repo": repo, "citations": len(rows), "problems": len(problems),
                      "by_kind": dict(Counter(r["kind"] for r in rows))},
            "matched": len(rows), "check": "fail" if problems else "pass", "results": shown,
            "omissions": {}, "next": ["Replace each problem with a cite token from context, edges, payload or raw, "
                                      "or a full commit SHA, and run cite again."] if problems else []}


def run(args):
    if args.action == "describe":
        return describe()
    if args.action == "cite":
        return lint_citations(args)
    if args.action in ("context", "payload", "raw"):
        args.ref = cite.ref_argument(args.ref, args.max_record_bytes)
        if getattr(args, "compare_ref", None):
            args.compare_ref = cite.ref_argument(args.compare_ref, args.max_record_bytes)
    if args.action == "duplicates":
        duplicates_query(args)
    if args.action == "projects":
        args.project = ""
        return projects(args)
    if args.action in ("payload", "raw"):
        return payload(args) if args.action == "payload" else raw(args)
    if args.action == "recurrence":
        # Terms match a failure's full output and its call's command after pairing, not the scan-time row text.
        args.terms, args.text = args.text, []
    if args.action == "context":
        args.project = None  # the ref fixes the file; a cwd default would drop it
        ref = args.ref
        args.files = [ref["file"]]
    elif args.project is None:
        # A query about another repository reads that repository's recordings, not the invocation directory's.
        args.project = os.path.abspath(args.repo) if getattr(args, "repo", None) else os.getcwd()
    if args.action == "select" and getattr(args, "include", None):
        # Repository views and recordings describe one project; the invocation directory is not implied.
        if not args.repo and args.project:
            args.repo = args.project
        elif args.repo and args.project and os.path.realpath(args.repo) != os.path.realpath(args.project):
            raise ValueError(f"--repo {args.repo} and --project {args.project} name different directories; "
                             "repository views would not match the recordings. Pass one, or the same path to both.")
    if args.action == "select" and args.sql and args.include and repository_only(args.sql):
        repository = Repository([], args, RecordCache(args.max_record_bytes))
        scope = {"recordings_read": False,
                 "recordings_basis": "query reads only repository views (commits, changes, files, code_items); "
                                     "linked commits outside --max-commits are not fetched without recordings"}
        result = sql_query([], scope, args, repository)
        if getattr(args, "builtin", None):
            # The shipped query text is in actions.DUPLICATES_SQL; echoing it would fill the budget.
            result["query"] = result["scope"]["query"] = args.builtin
            result["next"] = [n for n in result["next"] if isinstance(n, dict)]
        return result
    if args.max_candidates is None:
        args.max_candidates = CANDIDATES_PROJECT if args.project else CANDIDATES_DEFAULT
    if args.max_files is None:
        args.max_files = FILES_PROJECT if args.project else FILES_DEFAULT
    scope = {"duplicates_skipped": 0}
    discovery = DiscoveryOptions.from_args(args)
    groups = []
    for harness, files, gscope in candidate_files(discovery, scope):
        if args.action == "records":
            groups.append({"harness": harness, "scope": gscope, "files": files})
            continue
        scan = ScanOptions.from_args(args, text_budget(args, files, gscope))
        gscope["bytes_selected"] = sum(_size(path) for path in files)
        recordings = []
        for path in files:
            if gscope.get("event_limit_reached"):
                gscope["scan_complete"] = False
                break
            if gscope["bytes_scanned"] >= args.max_scan_bytes:
                gscope["scan_complete"] = False
                gscope["scan_limit_reached"] = True
                break
            before = gscope["bytes_scanned"]
            recordings.append(scan_file(path, scan, gscope))
            gscope["files_scanned"] += 1
            _snapshot(path, gscope, before)
        keys = lineages(recordings)
        gscope["sessions_independent"] = len(set(keys.values()))
        for r in recordings:
            for e in r["events"]:
                e["independent_session"] = keys[r["file"]]
        groups.append({"harness": harness, "scope": gscope, "events": recordings,
                       "recordings": [{"file": g["file"], "session": g["session"], "parent_session": g["parent_session"],
                                       "recorded_root": g["root"], "harness_version": g["harness_version"]}
                                      for g in recordings]})
    if args.action == "records":
        return scan_records(groups, args, combined_scope(scope, groups))
    if args.action == "edges":
        return edges(groups, combined_scope(scope, groups))
    if args.action == "context":
        recordings = [r for g in groups for r in g["events"]]
        return context(recordings, ref, args, combined_scope(scope, groups))
    if args.action == "recurrence":
        return recurrence(groups, args, combined_scope(scope, groups))
    repository = None
    if args.action == "select" and args.include:
        # Witnesses come from every scanned event, before any display filter.
        repository = Repository([r for g in groups for r in g["events"]], args, RecordCache(args.max_record_bytes))
    if args.action == "select" and args.sql:
        return sql_query(groups, combined_scope(scope, groups), args, repository)
    sections = []
    for g in groups:
        body = overview(g["events"], args) if args.action == "overview" else search(g["events"], args, repository)
        section = {"harness": g["harness"], "scope": g["scope"], **body}
        if args.action == "overview" or getattr(args, "recordings", False):
            # Search repeats the overview's recording list on every call; it is opt-in there.
            section["recordings"] = [{k: v for k, v in r.items() if v is not None} for r in g["recordings"]]
        sections.append(section)
    result = {"scope": combined_scope(scope, groups), "harnesses": sections}
    if args.action != "overview":
        # One list across harnesses: rank tiers interleave; time order keeps each harness's recorded order.
        merged = sorted((row for sec in sections for row in sec.pop("results")), key=lambda row: row["_order"])
        result["results"] = [{k: v for k, v in row.items() if k != "_order"} for row in merged]
    for sec in sections:
        sec["scope"].pop("snapshot", None)
    if repository is not None:
        result["scope"]["repository"] = repository.coverage
        if any(part.get("complete") is False for part in repository.coverage.values() if isinstance(part, dict)):
            result["status"] = "partial"
    result["scope"]["snapshot_basis"] = "refs are append-only; snapshot is the invocation-time view"
    recovery = coverage_next(groups, result["scope"])
    if recovery:
        # Recovery first: budget levels keep the earliest next entries.
        result["next"] = recovery
    if args.action == "overview":
        found = [n for sec in sections for n in sec.pop("_next")]
        hidden = sum(sec["omissions"]["requests_selection"] for sec in sections)
        if hidden:
            recovery = recovery + [all_requests(sum(sec["activity"]["request_count"] for sec in sections))]
        result["next"] = recovery + ([n for n in found if n["purpose"] == "Read this recorded turn"][:1]
                                     + [n for n in found if n["purpose"] != "Read this recorded turn"][:1])
        if any(g["scope"].get("files_excluded_project") for g in groups):
            result["next"].append({"purpose": "List recorded project roots, including historical ones",
                                   "argv": command_line([sys.executable, str(Path(__file__).resolve().parents[1]
                                                                           / "sessions.py"), "projects"])})
        if not any(g["scope"]["files_scanned"] for g in groups):
            result["notes"] = ["No file matched the project filter; widen with --project '' or explicit recordings paths."
                               if args.project else "No recordings were selected from the given paths."]
    else:
        result["matched"] = sum(sec["matched"] for sec in sections)
    return result
