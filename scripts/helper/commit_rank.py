"""Prior-work ranking for investigate: rank past commits against feature words, with linked requests and current path state.

Git commits are the durable unit; recordings only add the human request behind a linked commit.
Ranking is lexical and deterministic: each matched term weighs log(1 + commits / commits containing it),
so project-wide words count little; field multipliers are subject x3, body x1, best changed path x2,
request x2; +1 when any changed path still exists; ties go to the newer commit.
"""
import math
import re
from pathlib import Path

from .projection import human

STOPWORDS = frozenset("""a an and add added adds for from in into of on or the to with new make
    create small feature ui fix update via use using we our this that""".split())
SUBJECT, BODY, PATH, REQUEST, ALIVE = 3, 1, 2, 2, 1
ROWS, PATHS_SHOWN, REQUEST_CHARS = 10, 5, 200


def terms(text):
    """Lowercase alphanumeric words of 3+ chars, minus generic feature verbs; order kept, duplicates dropped."""
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 3 and w not in STOPWORDS]
    return list(dict.fromkeys(words))


def hits(words, text, weight=None):
    text = (text or "").lower()
    return sum(weight[w] if weight else 1 for w in words if w in text)


def current_paths(changes):
    """Map each historical path to its newest name, following later renames; None once deleted."""
    # changes arrive newest commit first; walk oldest to newest so later renames win.
    fate = {}
    for row in reversed(changes):
        repo = row.get("repo_id")
        if row.get("status") == "renamed" and row.get("old_path"):
            fate[(repo, row["old_path"])] = (repo, row.get("new_path") or row["path"])
        elif row.get("status") == "deleted":
            fate[(repo, row["path"])] = None
        elif (repo, row["path"]) in fate and fate[(repo, row["path"])] is None:
            fate[(repo, row["path"])] = (repo, row["path"])  # re-added later
    def resolve(path, repo=None):
        seen = set()
        key = (repo, path)
        while key in fate and key not in seen:
            seen.add(key)
            key = fate[key]
            if key is None:
                return None
        return key[1]
    return resolve


def requests(events, links):
    """(repo_id, sha) -> the latest human request before each linked commit command.

    A recording is the attribution boundary: a reused native session ID in another
    file cannot lend its request to this commit.
    """
    by_recording = {}
    for e in events:
        if human(e) and e.get("text"):
            key = (e.get("recording_id"), e.get("session"))
            by_recording.setdefault(key, []).append(e)
    found, stamps = {}, {}
    for link in links:
        sha = link.get("sha")
        if not sha:
            continue
        key = (link.get("recording_id"), link.get("session"))
        prior = [e for e in by_recording.get(key, [])
                 if (e.get("timestamp") or "") <= (link.get("timestamp") or "~")]
        if not prior:
            continue
        candidate = prior[-1]
        commit_key = (link.get("repo_id"), sha)
        stamp = candidate.get("timestamp") or ""
        if commit_key not in stamps or stamp >= stamps[commit_key]:
            found[commit_key] = (candidate["text"], candidate.get("cite"))
            stamps[commit_key] = stamp
            if link.get("repo_id") is None:
                found[sha] = found[commit_key]
    return found


def rank(text, commits, changes, events, links, root):
    words = terms(text)
    if not words:
        raise ValueError("GOAL needs descriptive words (3+ letters, not only generic verbs like 'add')")
    def ckey(row):
        return (row.get("repo_id"), row["sha"])
    paths, newest = {}, current_paths(changes)
    for row in changes:
        paths.setdefault(ckey(row), []).append(row.get("new_path") or row["path"])
    asked = requests(events, links)
    commits = [c for c in commits if not c.get("is_merge")]
    docs = {ckey(c): "\n".join([c.get("subject") or "", c.get("body") or "", *paths.get(ckey(c), [])]).lower()
            for c in commits}
    weight = {w: math.log(1 + len(docs) / max(1, sum(w in d for d in docs.values()))) for w in words}
    rows = []
    for commit in commits:
        sha = commit["sha"]
        key = ckey(commit)
        changed = paths.get(key, [])
        request, cite = asked.get(key) or asked.get(sha) or (None, None)
        score = (SUBJECT * hits(words, commit.get("subject"), weight) + BODY * hits(words, commit.get("body"), weight)
                 + PATH * max((hits(words, p, weight) for p in changed), default=0)
                 + REQUEST * hits(words, request, weight))
        if not score:
            continue
        states = []
        # Paths naming the feature first: they are the reuse candidates.
        for path in sorted(changed, key=lambda p: -hits(words, p)):
            now = newest(path, commit.get("repo_id"))
            exists = now is not None and (Path(root) / now).exists()
            states.append({"path": path, "now": now if exists else None,
                           "state": ("exists" if now == path else "moved") if exists else "gone"})
        if any(s["state"] != "gone" for s in states):
            score += ALIVE
        rows.append({"score": round(score, 1), "date": (commit.get("committed_at") or "")[:10], "sha": sha,
                     "subject": commit.get("subject"),
                     "paths": [s["path"] if s["state"] == "exists" else f"{s['path']} -> {s['now'] or 'gone'}"
                               for s in states[:PATHS_SHOWN]],
                     "paths_more": max(0, len(states) - PATHS_SHOWN),
                     "alive": sum(s["state"] != "gone" for s in states),
                     "request": request[:REQUEST_CHARS] if request else None, "cite": cite})
    rows.sort(key=lambda r: r["date"], reverse=True)
    rows.sort(key=lambda r: -r["score"])  # stable: equal scores stay newest first
    return words, rows[:ROWS]


def horizon(commits, sessions):
    """Oldest evidence actually scanned, so a miss can be read against the window."""
    dates = [c.get("committed_at") for c in commits if c.get("committed_at")]
    starts = [s.get("started_at") for s in sessions if s.get("started_at")]
    return {"commits": len(commits), "oldest_commit": min(dates)[:10] if dates else None,
            "sessions": len(sessions), "oldest_session": min(starts)[:10] if starts else None}
