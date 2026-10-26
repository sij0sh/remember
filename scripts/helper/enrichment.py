"""Repository tables beside session events, and the nested objects built from them.

Normalized rows are the single source: ``git`` and ``file`` objects on an event
are lookups into the same witness, commit and file rows that SQL sees, so every
enrichment is a left join. Unmatched events keep ``git: []`` and ``file: null``.
"""
import os

from . import commit_links, edit_hunks, git_file_history, repo_files
from .engine import Input
from .git_resolve import GitResolver

INCLUDES = ("git", "files", "symbols", "content", "edits")
COMMIT_KEYS = ("committed_at", "author_name", "author_email", "is_merge", "files_changed",
               "additions", "deletions")
FILE_KEYS = ("path", "exists", "file_type", "language", "size", "lines", "tracked", "index_status",
             "worktree_status", "blob_sha", "read_state")


def parse_include(value):
    names = {name.strip() for name in (value or "").split(",") if name.strip()}
    unknown = names - set(INCLUDES)
    if unknown:
        raise ValueError(f"Unknown --include {sorted(unknown)}; choose from {', '.join(INCLUDES)}")
    if "edits" in names:
        names.add("symbols")
    if names & {"symbols", "content"}:
        names.add("files")
    return names


def parse_root_map(values):
    """[(old, new)] normalized, longest OLD first so a nested mapping wins."""
    pairs = []
    for value in values or ():
        old, _, new = value.partition("=")
        pairs.append((os.path.normpath(os.path.expanduser(old)), os.path.normpath(os.path.expanduser(new))))
    return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)


def remap(path, pairs):
    """path with the first matching OLD prefix replaced by NEW; (path, mapped)."""
    if not path:
        return path, False
    normal = os.path.normpath(path)
    for old, new in pairs:
        if normal == old or normal.startswith(old.rstrip(os.sep) + os.sep):
            return new + normal[len(old):], True
    return path, False


def _relative(path, worktree):
    if not path or not worktree:
        return None
    real, root = os.path.realpath(path), os.path.realpath(worktree)
    if real != root and os.path.commonpath([real, root]) == root:
        return os.path.relpath(real, root).replace(os.sep, "/")
    return None


class Repository:
    """Tables for one invocation: witnesses from scanned events, git history, worktree files."""

    def __init__(self, recordings, args, cache):
        self.include = parse_include(args.include)
        self.resolver = GitResolver()
        self.worktree = self.resolver.repo(args.repo or os.getcwd())
        self.repo_id = self.resolver.identity(self.worktree)
        self.coverage = {"repo": self.worktree, "repo_id": self.repo_id, "include": sorted(self.include)}
        self.witnesses, self.commits, self.changes, self.files, self.items = [], [], [], [], []
        self.hunks, self.edit_symbols = [], []
        self.max_file_bytes = args.max_file_bytes
        self.root_map = parse_root_map(getattr(args, "root_map", []))
        self.mapped_events = set()
        if self.root_map:
            self.coverage["root_map"] = [{"old": old, "new": new} for old, new in self.root_map]
            self.coverage["root_mapped_events"] = 0
        if "git" in self.include:
            self._git(recordings, args, cache)
        if "files" in self.include and self.worktree:
            self.files, self.items, self.coverage["files"] = repo_files.snapshot(
                self.resolver, self.worktree, args.max_repo_files, args.max_file_bytes,
                content="content" in self.include, symbols="symbols" in self.include)
        elif "files" in self.include:
            self.coverage["files"] = {"complete": False, "error": "--repo is not inside a git worktree"}
        self.coverage["git_calls"] = self.resolver.calls
        self.coverage["git_errors"] = self.resolver.errors
        self.by_event, self.by_commit = {}, {(c["repo_id"], c["sha"]): c for c in self.commits}
        for row in self.witnesses:
            self.by_event.setdefault((row["recording_id"], row["event_id"]), []).append(row)
        self.by_path = {row["path"]: row for row in self.files}
        self.roots = {r["file"]: r.get("root") for r in recordings}
        self.items_by_path = {}
        for item in self.items:
            self.items_by_path.setdefault(item["path"], []).append(item)
        if "edits" in self.include:
            self._current = {}
            self.hunks, self.edit_symbols, self.coverage["edits"] = edit_hunks.derive(recordings, cache, self)

    def repo_path(self, event):
        """Worktree-relative path of an event's file, or None outside this worktree."""
        path, location = event.get("path"), event.get("path_location")
        if location == "repository":  # recorded relative to the recording's root
            root = self.roots.get(event.get("recording_id"))
            path = os.path.join(root, path) if root and path else None
        if location not in ("repository", "external"):
            return None
        path, mapped = remap(path, self.root_map)
        relative = _relative(path, self.worktree)
        if mapped and relative:
            self.mapped_events.add((event.get("recording_id"), event.get("event_id")))
            self.coverage["root_mapped_events"] = len(self.mapped_events)
        return relative

    def current_lines(self, path):
        """(today's lines, None) for a read worktree file, else (None, reason)."""
        if not path:
            return None, "outside_repo"
        if path not in self._current:
            row = self.by_path.get(path)
            if row is None or not row.get("exists", False):
                self._current[path] = (None, "file_missing")
            elif row.get("read_state") != "read" or row.get("binary"):
                self._current[path] = (None, "not_read")
            else:
                try:
                    with open(os.path.join(self.worktree, path), "rb") as stream:
                        text = stream.read(self.max_file_bytes + 1).decode("utf-8", "replace")
                    self._current[path] = (edit_hunks.split_lines(text), None)
                except OSError:
                    self._current[path] = (None, "not_read")
        return self._current[path]

    def has_items(self, path):
        return (self.by_path.get(path) or {}).get("parse_state") in ("parsed", "parsed_with_errors")

    def items_for(self, path):
        return self.items_by_path.get(path, [])

    def _git(self, recordings, args, cache):
        self.witnesses = commit_links.witnesses(recordings, self.resolver, cache, self.worktree)
        history = {"complete": False, "error": "--repo is not inside a git worktree"}
        if self.worktree:
            self.commits, self.changes, history = git_file_history.history(
                self.resolver, self.worktree, args.max_commits, args.follow)
        # Linked commits outside the logged range are fetched by SHA in their own repository.
        known = {(c["repo_id"], c["sha"]) for c in self.commits}
        wanted = {}
        for row in self.witnesses:
            if row["sha"] and row["repo"] and (row["repo_id"], row["sha"]) not in known:
                wanted.setdefault(row["repo"], set()).add(row["sha"])
        missing = 0
        for repo, shas in sorted(wanted.items()):
            commits, changes, absent = git_file_history.commits_by_sha(self.resolver, repo, shas)
            self.commits += commits
            self.changes += changes
            missing += len(absent)
        self.coverage["git"] = {**history, "witnesses": len(self.witnesses),
                                "unresolved_witnesses": sum(1 for r in self.witnesses if r["confidence"] == "unresolved"),
                                "inferred_witnesses": sum(1 for r in self.witnesses if r["confidence"] == "inferred"),
                                "linked_commits_fetched": sum(1 for c in self.commits if c["source"] == "linked"),
                                "linked_commits_missing": missing}

    def event_columns(self):
        """Columns enrichment adds to the events view, declared so empty selections bind."""
        columns = {"git": "JSON"} if "git" in self.include else {}
        if "files" in self.include:
            columns.update(file="JSON", repo_path="VARCHAR")
        return columns

    def enrich(self, event, row):
        """Add nested objects to one event row; one row per event regardless of matches."""
        if "git" in self.include:
            found = []
            for witness in self.by_event.get((event.get("recording_id"), event.get("event_id")), []):
                commit = self.by_commit.get((witness["repo_id"], witness["sha"]))
                found.append({**{k: witness[k] for k in ("sha", "recorded_sha", "repo", "subject", "basis",
                                                         "confidence", "failed", "command_offset")},
                              "commit": {k: commit.get(k) for k in COMMIT_KEYS} if commit else None})
            row["git"] = found
        if "files" in self.include:
            path = self.repo_path(event)
            row["repo_path"] = path
            match = self.by_path.get(path) if path else None
            row["file"] = {k: match.get(k) for k in FILE_KEYS} if match else None
        return row

    def inputs(self, types):
        """Declared views for every requested table, empty or not, after the events view."""
        tables = []
        if "git" in self.include:
            tables += [("commit_links", self.witnesses, commit_links.COLUMNS, commit_links.GRAIN, "git"),
                       ("commits", self.commits, git_file_history.COMMIT_COLUMNS, git_file_history.COMMIT_GRAIN, "git"),
                       ("changes", self.changes, git_file_history.CHANGE_COLUMNS, git_file_history.CHANGE_GRAIN, "git")]
        if "files" in self.include:
            tables.append(("files", self.files, repo_files.FILE_COLUMNS, repo_files.FILE_GRAIN, "files"))
        if "symbols" in self.include:
            tables.append(("code_items", self.items, repo_files.ITEM_COLUMNS, repo_files.ITEM_GRAIN, "files"))
        if "edits" in self.include:
            tables += [("edit_hunks", self.hunks, edit_hunks.HUNK_COLUMNS, edit_hunks.HUNK_GRAIN, "edits"),
                       ("edit_symbols", self.edit_symbols, edit_hunks.SYMBOL_COLUMNS, edit_hunks.SYMBOL_GRAIN,
                        "edits")]
        result = []
        for index, (name, rows, columns, grain, source) in enumerate(tables, start=1):
            coverage = self.coverage.get(source, {})
            declared = {**columns, **types(name, f"t{index}")}
            result.append(Input(name, rows, {"complete": coverage.get("complete", True)},
                                list(columns), declared, grain))
        return result
