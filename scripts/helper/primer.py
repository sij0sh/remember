"""Recent-work primer for bare map: bounded session and commit evidence, never retained.

The primer is an orientation aid, not a transcript view. It reads only enough of
each recent recording to recover the first substantive request and the last
substantive agent message, plus recorded file edits. Long recordings are read
from the head and tail; the middle is deliberately skipped. Git commits come
from one read-only log call. Nothing here writes to the graph or report cache.
"""
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .actions import lineages
from .discovery import candidate_files
from .git_file_history import history
from .git_resolve import GitResolver
from .options import DiscoveryOptions, ScanOptions
from .reader import scan_file
from .records import new_scope

HEAD_BYTES = 1_000_000
TAIL_BYTES = 8_000_000
FULL_SCAN_BYTES = HEAD_BYTES + TAIL_BYTES
MAX_SCAN_BYTES = 48_000_000
MAX_EVENTS = 20_000
TEXT_BYTES = 512 * 1024
PREVIEW_CHARS = 400
MAX_DEPTH = 10
MAX_CANDIDATES = 20_000

INJECT = (
    r"<environment_context>.*?</environment_context>",
    r"<command-name>.*?</command-name>",
    r"<command-message>.*?</command-message>",
    r"<system-reminder>.*?</system-reminder>",
    r"<user_instructions>.*?</user_instructions>",
    r"<local-command-stdout>.*?</local-command-stdout>",
    r"(?m)^#\s*(AGENTS|CLAUDE)\.md\b.*$",
    r"(?m)^\.\s*(?:zsh|bash)\s+\d{4}-\d{2}-\d{2}.*$",
)
INJECT_RE = tuple(re.compile(p, re.S | re.I) for p in INJECT)
GENERIC = {
    "yes", "y", "ok", "okay", "continue", "proceed", "go on", "go ahead", "do it",
    "sounds good", "lgtm", "try again", "retry", "next", "please continue",
    "continue.", "proceed.", "yes please", "go for it",
}
FENCE = re.compile(r"```.*?```", re.S)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")
QUOTE = re.compile(r"(?m)^\s*>\s?")
BOLD = re.compile(r"\*\*([^*]+)\*\*")
UNDER = re.compile(r"__([^_]+)__")
ITALIC = re.compile(r"(?<![\w*_])[*_]([^*_\n]+)[*_](?![\w*_])")
LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
HTML = re.compile(r"</?[A-Za-z][^>]*>")
PATH_ONLY = re.compile(r"^[\w./\\@+-]+$")


def _clean(text):
    """Plain, single-line prose from recorded markdown/HTML without removing identifiers."""
    if not text:
        return ""
    text = str(text)
    text = ANSI.sub(" ", text)
    text = FENCE.sub(" [code] ", text)
    text = LINK.sub(r"\1", text)
    text = HTML.sub(" ", text)
    text = HEADING.sub("", text)
    text = QUOTE.sub("", text)
    text = BOLD.sub(r"\1", text)
    text = UNDER.sub(r"\1", text)
    text = ITALIC.sub(r"\1", text)
    text = text.replace("`", "")
    text = text.replace("\u2014", "-").replace("\u2013", "-").replace("\u2026", "...")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = CONTROL.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n-|")
    return text


def _visible(text):
    """Remove harness-injected wrappers before deciding whether a message is a request."""
    if not text:
        return ""
    for pattern in INJECT_RE:
        text = pattern.sub(" ", str(text))
    return _clean(text)


def _clip(text, limit):
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 3]
    for mark in (". ", "; ", "? ", "! "):
        pos = cut.rfind(mark)
        if pos >= max(20, limit // 2):
            return cut[:pos + 1].rstrip() + "..."
    pos = cut.rfind(" ")
    if pos > 0:
        cut = cut[:pos]
    return cut.rstrip(" ,;:-") + "..."


def _short_path(path, limit=60):
    value = str(path).replace("\\", "/")
    if len(value) <= limit:
        return value
    parts = [p for p in value.split("/") if p]
    for count in (3, 2, 1):
        suffix = ".../" + "/".join(parts[-count:])
        if len(suffix) <= limit:
            return suffix
    return value[:limit - 3].rstrip("/") + "..."


def _time(value):
    if not value:
        return None
    try:
        found = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return found.replace(tzinfo=timezone.utc) if found.tzinfo is None else found


def _age(value):
    found = _time(value)
    if found is None:
        return "?"
    seconds = max(0, (datetime.now(timezone.utc) - found).total_seconds())
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    if seconds < 86400 * 60:
        return f"{int(seconds // 86400)}d"
    if seconds < 86400 * 365:
        return f"{int(seconds // (86400 * 30))}mo"
    return f"{seconds / (86400 * 365):.1f}y".replace(".0y", "y")


def _iso(value):
    found = _time(value)
    return found.isoformat().replace("+00:00", "Z") if found else None


def _request(text):
    """A human request, not a harness wrapper, slash command, or bare acknowledgement."""
    value = _visible(text)
    if len(value) < 15:
        return False
    low = value.casefold().strip(" .!?:;")
    if low in GENERIC or not low:
        return False
    if value.startswith("/") and " " not in value:
        return False
    if PATH_ONLY.fullmatch(value) and len(value) < 120:
        return False
    return True


def _message(text, minimum=80):
    value = _clean(text)
    return value if len(value) >= minimum else ""


def _summary_goal(text):
    """The objective line of a compaction/checkpoint summary, else its opening prose."""
    raw = str(text or "")
    for pattern in (r"(?im)^#{1,6}\s*Objective\s*\n+([^\n#]+)",
                    r"(?im)^\s*[-*]?\s*Objective\s*:\s*([^\n]+)"):
        found = re.search(pattern, raw)
        if found:
            value = _clean(found.group(1))
            if value:
                return value
    return _clean(raw)


def _ranges(size):
    if size <= FULL_SCAN_BYTES:
        return None
    tail = max(HEAD_BYTES, size - TAIL_BYTES)
    return [(0, HEAD_BYTES), (tail, None)]


def _read_session(path, harness):
    scope = new_scope()
    ranges = _ranges(Path(path).stat().st_size)
    recording = scan_file(path, ScanOptions(harness=harness, max_record_bytes=2 * 1024 * 1024,
                                            max_scan_bytes=MAX_SCAN_BYTES, max_events=MAX_EVENTS,
                                            max_text_bytes=TEXT_BYTES, preview_chars=PREVIEW_CHARS),
                         scope, ranges=ranges)
    recording["_scan"] = {"sampled": bool(ranges), "scan_complete": bool(scope.get("scan_complete", True)),
                          "text_complete": bool(scope.get("text_complete", True)),
                          "bytes_scanned": scope.get("bytes_scanned", 0),
                          "records_scanned": scope.get("records_scanned", 0)}
    return recording


def _events(recordings):
    events = [event for recording in recordings for event in recording["events"]]
    events.sort(key=lambda e: (e.get("order") if isinstance(e.get("order"), int) else 0,
                               e.get("ref", {}).get("offset", 0)))
    return events


def _session_rows(recordings, depth, excluded):
    keys = lineages(recordings)
    groups = defaultdict(list)
    kept = 0
    for recording in recordings:
        key = keys[recording["file"]]
        if {recording["session"], recording["file"], key} & excluded:
            continue
        groups[key].append(recording)
        kept += 1
    rows = []
    for key, group in groups.items():
        roots = [r for r in group if not r.get("parent_session")] or group
        events = _events(roots)
        goal = next((e for e in events if e.get("kind") == "user_message"
                     and e.get("origin") == "human" and not e.get("parent_session")
                     and _request(e.get("text"))), None)
        summaries = [e for e in events if e.get("kind") == "summary" and _message(e.get("text"), 40)]
        substantive = [e for e in events if e.get("kind") == "assistant_message"
                       and e.get("origin") == "assistant" and _message(e.get("text"), 80)]
        last = max(substantive + summaries, key=lambda e: e.get("order", 0), default=None)
        if last is None:
            assistants = [e for e in events if e.get("kind") == "assistant_message"
                          and e.get("origin") == "assistant" and _clean(e.get("text"))]
            last = assistants[-1] if assistants else None
        goal_text = _clean(goal.get("text")) if goal else ""
        if not goal_text and summaries:
            goal_text = _summary_goal(summaries[-1].get("text"))
        last_text = _clean(last.get("text")) if last else ""
        if goal_text and last_text and goal_text == last_text:
            last = None
            last_text = ""
        edited, latest = {}, {}
        for event in events:
            if event.get("operation") == "file_edit" and event.get("path"):
                path = str(event["path"]).replace("\\", "/")
                edited[path] = edited.get(path, 0) + 1
                latest[path] = max(latest.get(path, 0), event.get("order", 0))
        paths = sorted(edited, key=lambda p: (-latest[p], p))
        if not (goal_text or last_text or paths):
            continue
        stamps = [_time(e.get("timestamp")) for e in events]
        stamps = [stamp for stamp in stamps if stamp]
        if not stamps:
            try:
                stamps = [datetime.fromtimestamp(Path(roots[0]["file"]).stat().st_mtime, timezone.utc)]
            except OSError:
                stamps = []
        ended = max(stamps) if stamps else None
        rows.append({"independent_session": key, "harness": roots[0].get("harness", "unknown"),
                     "ended_at": _iso(ended) if ended else None,
                     "goal": goal_text, "last": last_text,
                     "last_kind": "summary" if last is not None and last.get("kind") == "summary" else "last",
                     "edited": [{"path": p, "edits": edited[p]} for p in paths],
                     "recording": roots[0]["file"], "root": roots[0].get("root")})
    rows.sort(key=lambda r: r.get("ended_at") or "", reverse=True)
    return rows[:depth], kept, len(groups)


def _discover(roots, depth, excluded):
    scope = {"duplicates_skipped": 0}
    try:
        groups = candidate_files(DiscoveryOptions(project=roots[0], also_roots=tuple(roots[1:]),
                                                  max_files=depth * 2 + 2, max_candidates=MAX_CANDIDATES),
                                 scope)
    except (OSError, ValueError) as error:
        return [], {"state": "unavailable", "reason": str(error)[:240], "roots": roots,
                    "current_session_exclusion": "configured" if excluded else "unknown; pass --exclude-session"}
    recordings, group_rows = [], []
    for harness, files, counters in groups:
        scanned = []
        for path in files:
            try:
                recording = _read_session(path, harness)
            except (OSError, ValueError):
                continue
            scanned.append(recording)
            recordings.append(recording)
        group_rows.append({"harness": harness, "files_selected": len(files), "files_scanned": len(scanned),
                           "files_excluded_project": counters.get("files_excluded_project", 0),
                           "project_unknown": counters.get("project_unknown", 0)})
    rows, kept, total_sessions = _session_rows(recordings, depth, excluded)
    scans = [recording["_scan"] for recording in recordings]
    sampled = any(scan["sampled"] for scan in scans)
    # Preview clipping is intentional. Only discovery or scan limits make the lane partial.
    partial = (not scope.get("discovery_complete", True)
               or any(not scan["scan_complete"] and not scan["sampled"] for scan in scans))
    return rows, {
        "state": "partial" if partial else ("sampled" if sampled else "searched"),
        "roots": roots,
        "recordings_scanned": len(recordings),
        "recordings_kept": kept,
        "recordings_excluded": len(recordings) - kept,
        "sessions": len(rows),
        "sessions_total": total_sessions,
        "has_more": total_sessions > len(rows),
        "sampled": sampled,
        "text_truncated": sum(1 for scan in scans if not scan["text_complete"]),
        "discovery_complete": bool(scope.get("discovery_complete", True)),
        "candidates_found": scope.get("candidates_found", 0),
        "independent_sessions": len({r["independent_session"] for r in rows}),
        "current_session_exclusion": "configured" if excluded else "unknown; pass --exclude-session",
        "selection_order": "recording mtime, then first request/last message; not semantic chronology",
        "groups": group_rows,
    }


def _exclusions(values):
    excluded = {str(value) for value in values or () if value}
    for name in ("CODEX_THREAD_ID", "PI_SESSION_ID", "CLAUDE_SESSION_ID"):
        value = os.environ.get(name)
        if value:
            excluded.add(value)
    current = os.environ.get("PI_SESSION_FILE")
    if current:
        excluded.add(str(Path(current).absolute()))
        excluded.add(os.path.realpath(current))
    return excluded


def _session_lane(root, depth, explicit_roots, exclude):
    roots = [str(Path(value).expanduser().resolve()) for value in explicit_roots] if explicit_roots else [root]
    rows, coverage = _discover(roots, depth, exclude)
    if not rows and not explicit_roots:
        # Recordings are keyed by the cwd the harness saw. A skill nested under a
        # project is often recorded at an ancestor; walk a few levels before giving up.
        current = Path(root)
        for level in range(1, 4):
            parent = current.parent
            if parent == current:
                break
            current = parent
            retry, retry_coverage = _discover([str(parent)], depth, exclude)
            if retry:
                retry_coverage["fallback"] = f"{level} parent director{'y' if level == 1 else 'ies'}"
                return retry, retry_coverage
    return rows, coverage


def _commit_lane(root, depth, include_merges):
    resolver = GitResolver()
    repo = resolver.repo(root)
    if not repo:
        return [], {"state": "unavailable", "reason": "not a Git repository", "repo": None}
    commits, changes, coverage = history(resolver, repo, depth, no_merges=not include_merges)
    if coverage.get("error"):
        return [], {"state": "searched", "repo": repo, "complete": True, "empty": True,
                    "reason": coverage["error"], "commits_limit": depth,
                    "merges_included": include_merges, "merges_skipped": 0}
    by_sha = defaultdict(list)
    for change in changes:
        by_sha[change["sha"]].append(change)
    rows = []
    for commit in commits[:depth]:
        paths = sorted(by_sha.get(commit["sha"], []),
                       key=lambda c: (-((c.get("additions") or 0) + (c.get("deletions") or 0)), c.get("path") or ""))
        rows.append({"sha": commit["sha"], "committed_at": commit.get("committed_at"),
                     "subject": _clean(commit.get("subject")), "is_merge": commit.get("is_merge", False),
                     "files_changed": commit.get("files_changed", 0),
                     "additions": commit.get("additions", 0), "deletions": commit.get("deletions", 0),
                     "paths": [{"path": c.get("path"), "additions": c.get("additions"),
                                "deletions": c.get("deletions")} for c in paths[:2]]})
    skipped = 0
    if not include_merges:
        found = resolver.run(repo, "log", "-n", str(depth), "--merges", "--format=%H", "HEAD")
        skipped = len([line for line in (found or "").splitlines() if line.strip()])
    return rows, {"state": "searched", "repo": repo, "complete": coverage.get("complete", False),
                  "commits_limit": depth, "merges_included": include_merges,
                  "merges_skipped": skipped, "empty": not bool(rows)}


def _tree_state(graph):
    git = (graph.body.get("source") or {}).get("git") or {}
    if not git.get("available") or git.get("status") is None:
        return None
    lines = [line for line in str(git["status"]).splitlines() if line.strip()]
    untracked = sum(1 for line in lines if line.startswith("??"))
    return {"changed": len(lines) - untracked, "untracked": untracked}


def build(graph, args):
    """Collect the structured primer. Callers decide whether to render or retain it."""
    root = str(Path(graph.body["source"]["root"]).resolve())
    depth = max(0, min(MAX_DEPTH, int(getattr(args, "primer_depth", 3) or 0)))
    if not depth:
        return {"sessions": [], "commits": [], "tree": None,
                "coverage": {"sessions": {"state": "disabled", "sessions": 0},
                             "commits": {"state": "disabled"}}}
    exclude = _exclusions(getattr(args, "exclude_session", None))
    sessions, session_coverage = _session_lane(root, depth, getattr(args, "primer_root", None), exclude)
    commits, commit_coverage = _commit_lane(root, depth, bool(getattr(args, "primer_merges", False)))
    return {"sessions": sessions, "commits": commits, "tree": _tree_state(graph),
            "coverage": {"sessions": session_coverage, "commits": commit_coverage}}


def _header(data, session_count=None, commit_count=None):
    sessions = data.get("sessions", [])
    commits = data.get("commits", [])
    session_count = len(sessions) if session_count is None else session_count
    commit_count = len(commits) if commit_count is None else commit_count
    coverage = data.get("coverage", {})
    session_coverage = coverage.get("sessions", {})
    commit_coverage = coverage.get("commits", {})
    flags = []
    if session_coverage.get("state") in ("partial", "sampled"):
        flags.append("sessions sampled" if session_coverage.get("sampled") else "sessions partial")
    if session_coverage.get("current_session_exclusion") == "configured":
        flags.append("current session excluded")
    else:
        flags.append("current may be included")
    head = "recent work (recorded"
    if flags:
        head += "; " + "; ".join(flags)
    head += f"): {session_count}{'+' if session_coverage.get('has_more') and session_count else ''} "
    head += f"session{'s' if session_count != 1 else ''}, "
    head += f"{commit_count}{'+' if commit_coverage.get('complete') is False else ''} "
    head += f"commit{'s' if commit_count != 1 else ''}"
    return head


def _sections(data):
    sessions = data.get("sessions", [])
    commits = data.get("commits", [])
    coverage = data.get("coverage", {})
    session_coverage = coverage.get("sessions", {})
    commit_coverage = coverage.get("commits", {})
    sections = []
    if sessions:
        sections.append({"kind": "sessions", "lines": ["  sessions:"]})
        for row in sessions:
            harness = {"claude-code": "claude"}.get(row.get("harness"), row.get("harness") or "unknown")
            goal = _clip(row.get("goal") or "(no recorded request)", 140)
            lines = [f"  - {_age(row.get('ended_at'))} [{harness}] {goal}"]
            optional = []
            if row.get("last"):
                label = "summary" if row.get("last_kind") == "summary" else "last"
                lines.append(f"      {label}: {_clip(row['last'], 180)}")
                optional.append(len(lines) - 1)
            edited = row.get("edited") or []
            if edited:
                shown = ", ".join(_short_path(item["path"]) for item in edited[:3])
                more = len(edited) - min(3, len(edited))
                lines.append(f"      edited: {shown}" + (f" (+{more})" if more else ""))
                optional.append(len(lines) - 1)
            sections.append({"kind": "session", "lines": lines, "optional": optional})
    elif session_coverage.get("state") == "unavailable":
        sections.append({"kind": "sessions", "lines": [
            f"  sessions: unavailable ({_clip(session_coverage.get('reason', 'no recordings'), 100)})"]})
    else:
        sections.append({"kind": "sessions", "lines": ["  sessions: none recorded for this root"]})
    if commits:
        sections.append({"kind": "commits", "lines": ["  commits:"]})
        for row in commits:
            subject = _clip(row.get("subject") or "(no subject)", 80)
            stats = f"{row.get('files_changed', 0)} files +{row.get('additions', 0)}/-{row.get('deletions', 0)}"
            sections.append({"kind": "commit", "lines": [
                f"  - {(row.get('sha') or '')[:8]} {_age(row.get('committed_at'))} {subject}  ({stats})"],
                "optional": []})
            if row.get("paths"):
                paths = ", ".join(_short_path(item.get("path") or "") for item in row["paths"])
                sections[-1]["lines"].append(f"      top: {paths}")
                sections[-1]["optional"].append(1)
    elif commit_coverage.get("state") == "unavailable":
        sections.append({"kind": "commits", "lines": ["  commits: unavailable (not a Git repository)"]})
    else:
        sections.append({"kind": "commits", "lines": ["  commits: none on HEAD"]})
    tree = data.get("tree")
    if tree:
        state = "clean" if not tree["changed"] and not tree["untracked"] else (
            f"{tree['changed']} changed, {tree['untracked']} untracked")
        sections.append({"kind": "tree", "lines": [f"  tree: {state}"]})
    return sections


def _joined(header, sections):
    lines = [header]
    for section in sections:
        lines.extend(section["lines"])
    return "\n".join(lines) + "\n"


def render(data, max_bytes=2048):
    """Render a primer to text lines, trimming optional detail before whole rows."""
    if not data or (not data.get("sessions") and not data.get("commits") and not data.get("tree")):
        return []
    header = _header(data)
    sections = _sections(data)
    while len(_joined(header, sections).encode("utf-8")) > max_bytes:
        changed = False
        for section in reversed(sections):
            if section.get("optional"):
                section["lines"].pop(section["optional"].pop())
                changed = True
                break
        if changed:
            continue
        for kind in ("session", "commit", "tree"):
            index = next((i for i in range(len(sections) - 1, -1, -1) if sections[i]["kind"] == kind), None)
            if index is not None:
                sections.pop(index)
                changed = True
                break
        if not changed:
            break
    # Drop a now-empty section heading.
    if not any(section["kind"] == "session" for section in sections):
        sections = [section for section in sections if section["kind"] != "sessions"]
    if not any(section["kind"] == "commit" for section in sections):
        sections = [section for section in sections if section["kind"] != "commits"]
    header = _header(data, sum(1 for section in sections if section["kind"] == "session"),
                     sum(1 for section in sections if section["kind"] == "commit"))
    text = _joined(header, sections)
    return text.rstrip("\n").splitlines()
