"""Bare-map orientation: the investigate packet with no goal, plus recent commits and tree state.

One selector serves investigate, inspect and map (D59): the archive is read through the
same scheduled scan, so a lead here has the same ID as in `investigate`. Nothing is retained.
The active session's own leads are left out: orientation is about prior work.
"""
from collections import defaultdict
from datetime import datetime, timezone

from .git_file_history import history
from .git_resolve import GitResolver

COMMITS = 3
SECONDS = 8.0
CATEGORIES = ("pitfall", "invariant", "task", "decision")


def clip(text, limit):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 3]
    pos = cut.rfind(" ")
    return (cut[:pos] if pos > limit // 2 else cut).rstrip(" ,;:-") + "..."


def age(value):
    try:
        found = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "?"
    if found.tzinfo is None:
        found = found.replace(tzinfo=timezone.utc)
    seconds = max(0, (datetime.now(timezone.utc) - found).total_seconds())
    for size, unit in ((86400 * 365, "y"), (86400 * 30, "mo"), (86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size * (2 if unit == "mo" else 1):
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s"


def commits(root):
    """Newest non-merge commits on HEAD with file/line totals and the two largest paths."""
    resolver = GitResolver()
    repo = resolver.repo(root)
    if not repo:
        return [], {"state": "unavailable", "reason": "not a Git repository"}
    found, changes, coverage = history(resolver, repo, COMMITS, no_merges=True)
    if coverage.get("error"):
        return [], {"state": "searched", "reason": coverage["error"]}
    by_sha = defaultdict(list)
    for change in changes:
        by_sha[change["sha"]].append(change)
    rows = []
    for commit in found[:COMMITS]:
        paths = sorted(by_sha.get(commit["sha"], []),
                       key=lambda c: (-((c.get("additions") or 0) + (c.get("deletions") or 0)), c.get("path") or ""))
        rows.append({"sha": commit["sha"], "committed_at": commit.get("committed_at"),
                     "subject": commit.get("subject"), "files_changed": commit.get("files_changed", 0),
                     "additions": commit.get("additions", 0), "deletions": commit.get("deletions", 0),
                     "paths": [c.get("path") for c in paths[:2]]})
    return rows, {"state": "searched", "complete": coverage.get("complete", False)}


def tree(graph):
    git = (graph.body.get("source") or {}).get("git") or {}
    if not git.get("available") or git.get("status") is None:
        return None
    lines = [line for line in str(git["status"]).splitlines() if line.strip()]
    untracked = sum(line.startswith("??") for line in lines)
    return {"changed": len(lines) - untracked, "untracked": untracked}


def _investigate_args(args, store):
    """Full investigate defaults (standard budget) carrying map's recording scope."""
    from .runtime import normalize, parser
    argv = ["investigate", "--repo", str(store.root), "--cache", str(store.path.parent)]
    for flag, values in (("--recordings", args.recordings), ("--project-root", args.project_root),
                         ("--exclude-session", args.exclude_session)):
        for value in values or ():
            argv += [flag, value]
    return normalize(parser().parse_args(argv), argv)


def packet(args, store):
    """(views, first-round leads, coverage) from the shared selector."""
    from . import investigate
    from .budget import Deadline
    from .providers import Providers
    options = _investigate_args(args, store)
    with Deadline(SECONDS) as deadline:
        providers = Providers(options, store, deadline)
        found, active = investigate.collect(options, providers, deadline, "", [], [])
        leads = [lead for lead in found.leads.values() if not lead.get("active_context")]
        views = investigate.views(leads, providers, True)
        picked, seen = [], set()
        for lead in investigate.order(leads, []):
            category = next((c for c in lead["categories"] if c not in seen), None)
            if category:
                seen.add(category)
                picked.append({"category": category, "id": lead["id"], "excerpt": lead.get("excerpt"),
                               "ref": lead.get("ref")})
        recordings = providers.coverage.get("recordings", {})
    exclusion = "configured" if active.get("file") or options.exclude_session else "unknown"
    return views, picked, {"state": recordings.get("state"), "complete": recordings.get("complete"),
                           "current_session_exclusion": exclusion, "active": active.get("state")}


def build(graph, args, store):
    """Structured orientation; callers render or attach it."""
    views, leads, coverage = packet(args, store)
    rows, commit_coverage = commits(str(store.root))
    return {"views": {k: {"state": v["state"], "eligible": v["eligible"]} for k, v in views.items()},
            "leads": leads, "commits": rows, "tree": tree(graph),
            "coverage": {"sessions": coverage, "commits": commit_coverage}}


def render(data, max_bytes=2048):
    """Text lines in priority order; lowest-priority lines drop first and the drop is named."""
    if not data["leads"] and not data["commits"] and not data.get("tree"):
        return ["primer omitted: no leads, commits or tree state"]
    sessions = data["coverage"]["sessions"]
    flags = ["current session excluded" if sessions.get("current_session_exclusion") == "configured"
             else "current may be included"]
    if sessions.get("complete") is False:
        flags.append("archive partial")
    header = f"recent work ({'; '.join(flags)}): " + " ".join(
        f"{k}={v['state']}({v['eligible']})" for k, v in data["views"].items())
    leads = [f"  {lead['category']}: {clip(lead['excerpt'], 150)}  [{lead['ref']}]" for lead in data["leads"]]
    shown = [f"  - {row['sha'][:8]} {age(row.get('committed_at'))} {clip(row.get('subject'), 80)}"
             f"  ({row['files_changed']} files +{row['additions']}/-{row['deletions']})" for row in data["commits"]]
    if not shown:
        reason = data["coverage"]["commits"].get("reason")
        shown = [f"  commits: {'unavailable (' + reason + ')' if reason else 'none on HEAD'}"]
    state = data.get("tree")
    tail = [] if not state else ["  tree: " + ("clean" if not state["changed"] and not state["untracked"]
                                             else f"{state['changed']} changed, {state['untracked']} untracked")]
    # Drop order: last commit first, then last lead; header and tree stay.
    middle, dropped = [*leads, *shown], 0
    def size():
        return len(("\n".join([header, *middle, *tail]) + "\n").encode("utf-8"))
    while middle and size() > max_bytes:
        middle.pop()
        dropped += 1
    body = [header, *middle, *tail]
    if dropped:
        body.append(f"  primer omitted: byte cap (--primer-bytes {max_bytes}); {dropped} lines not shown;"
                    " run investigate for all leads")
    return body
