"""Normalized commits and file changes from read-only ``git log``.

One commit row per repository commit and one change row per commit/file pair,
always stated against ``parent_sha``. Merges diff against their first parent, so
``changes`` describes what the merge brought onto that line; ``parents`` keeps
every parent. Renames keep both paths. Patches are not extracted.
"""
RECORD, FIELD = "\x1e", "\x1f"
FORMAT = RECORD + FIELD.join(("%H", "%P", "%an", "%ae", "%aI", "%cn", "%ce", "%cI", "%D", "%s", "%b")) + FIELD
HEADER_FIELDS = 11
MAX_BODY = 4000

COMMIT_GRAIN = "one repository commit"
CHANGE_GRAIN = "one commit/file change against parent_sha"
COMMIT_COLUMNS = {"repo_id": "VARCHAR", "repo": "VARCHAR", "sha": "VARCHAR", "parents": "JSON",
                  "parent_count": "BIGINT", "is_merge": "BOOLEAN", "author_name": "VARCHAR",
                  "author_email": "VARCHAR", "authored_at": "VARCHAR", "committer_name": "VARCHAR",
                  "committer_email": "VARCHAR", "committed_at": "VARCHAR", "decorations": "VARCHAR",
                  "subject": "VARCHAR", "body": "VARCHAR", "body_omitted_chars": "BIGINT",
                  "files_changed": "BIGINT", "additions": "BIGINT", "deletions": "BIGINT",
                  "source": "VARCHAR"}
CHANGE_COLUMNS = {"repo_id": "VARCHAR", "sha": "VARCHAR", "parent_sha": "VARCHAR", "path": "VARCHAR",
                  "old_path": "VARCHAR", "new_path": "VARCHAR", "status": "VARCHAR",
                  "similarity": "BIGINT", "additions": "BIGINT", "deletions": "BIGINT",
                  "binary": "BOOLEAN", "old_mode": "VARCHAR", "new_mode": "VARCHAR"}
STATUS = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "copied",
          "T": "type_changed", "U": "unmerged", "X": "unknown"}


def _diff(rest):
    """Pair -z --raw entries with -z --numstat entries; both list files in the same order."""
    tokens = [token.lstrip("\n") for token in rest.split("\0")]
    raw, stats, i = [], [], 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith(":"):
            old_mode, new_mode, _, _, status = token[1:].split(" ", 4)
            if status[:1] in ("R", "C"):
                raw.append((old_mode, new_mode, status, tokens[i + 1], tokens[i + 2]))
                i += 3
            else:
                raw.append((old_mode, new_mode, status, tokens[i + 1], tokens[i + 1]))
                i += 2
        elif token.count("\t") >= 2:
            added, deleted, path = token.split("\t", 2)
            # A rename's numstat leaves the path empty and spells old/new as separate tokens.
            i += 3 if not path else 1
            stats.append((added, deleted))
        else:
            i += 1
    return [(entry, stats[n] if n < len(stats) else ("-", "-")) for n, entry in enumerate(raw)]


def _parse(raw, repo_id, repo, source):
    commits, changes = [], []
    for chunk in raw.split(RECORD):
        if not chunk.strip():
            continue
        fields = chunk.split(FIELD, HEADER_FIELDS)
        if len(fields) != HEADER_FIELDS + 1:
            continue
        sha, parents, an, ae, ad, cn, ce, cd, refs, subject, body, rest = fields
        parents = parents.split()
        body = body.strip()
        commit = {"repo_id": repo_id, "repo": repo, "sha": sha, "parents": parents,
                  "parent_count": len(parents), "is_merge": len(parents) > 1,
                  "author_name": an, "author_email": ae, "authored_at": ad,
                  "committer_name": cn, "committer_email": ce, "committed_at": cd,
                  "decorations": refs or None, "subject": subject, "body": body[:MAX_BODY] or None,
                  "body_omitted_chars": max(0, len(body) - MAX_BODY), "source": source}
        added_total = deleted_total = 0
        files = _diff(rest)
        for (old_mode, new_mode, status, old, new), (added, deleted) in files:
            binary = added == "-"
            added, deleted = (None, None) if binary else (int(added), int(deleted))
            added_total += added or 0
            deleted_total += deleted or 0
            code = status[:1]
            changes.append({"repo_id": repo_id, "sha": sha, "parent_sha": parents[0] if parents else None,
                            "path": old if code == "D" else new,
                            "old_path": None if code == "A" else old,
                            "new_path": None if code == "D" else new,
                            "status": STATUS.get(code, "unknown"),
                            "similarity": int(status[1:]) if status[1:].isdigit() else None,
                            "additions": added, "deletions": deleted, "binary": binary,
                            "old_mode": None if code == "A" else old_mode,
                            "new_mode": None if code == "D" else new_mode})
        commit.update(files_changed=len(files), additions=added_total, deletions=deleted_total)
        commits.append(commit)
    return commits, changes


def _log(resolver, repo, *args):
    return resolver.run(repo, "log", "-z", "--no-color", "--no-ext-diff", "--no-abbrev", "--find-renames",
                        "--diff-merges=first-parent", "--raw", "--numstat", f"--format={FORMAT}", *args,
                        timeout=60)


def history(resolver, repo, max_commits, follow=None):
    """Commits reachable from HEAD, newest first; ``follow`` narrows to one path across renames."""
    repo_id = resolver.identity(repo)
    args = ["-n", str(max_commits + 1), "HEAD"]
    if follow:
        args += ["--follow", "--", follow]
    raw = _log(resolver, repo, *args)
    if raw is None:
        return [], [], {"complete": False, "error": "git log failed (no commits or not a repository)"}
    commits, changes = _parse(raw, repo_id, repo, "follow" if follow else "head_log")
    complete = len(commits) <= max_commits
    if not complete:
        dropped = commits[max_commits:]
        commits = commits[:max_commits]
        gone = {c["sha"] for c in dropped}
        changes = [c for c in changes if c["sha"] not in gone]
    return commits, changes, {"complete": complete, "commits_limit": max_commits}


def commits_by_sha(resolver, repo, shas):
    """Specific linked commits, verified first so one missing SHA cannot fail the batch."""
    shas = sorted(set(shas))
    if not shas:
        return [], [], []
    out = resolver.run(repo, "cat-file", "--batch-check=%(objectname) %(objecttype)", stdin="\n".join(shas) + "\n")
    present = [line.split()[0] for line in (out or "").splitlines() if line.endswith(" commit")]
    missing = sorted(set(shas) - set(present))
    if not present:
        return [], [], missing
    raw = _log(resolver, repo, "--no-walk=unsorted", *present)
    commits, changes = _parse(raw or "", resolver.identity(repo), repo, "linked")
    return commits, changes, missing
