"""Read-only local git execution, resolution, caching and limits shared by every git table."""
import os
import re
import subprocess
from pathlib import Path

from .git_commands import normalize_subject, SHA


class GitResolver:
    """Read-only local git lookups, cached by recorded command directory."""

    def __init__(self, override=None, enabled=True):
        self.override = override
        self.enabled = enabled
        self.repos = {}
        self.subjects = {}
        self.identities = {}
        self.errors = 0
        self.calls = 0

    def _run(self, cwd, *args, stdin=None, binary=False, timeout=15):
        self.calls += 1
        try:
            result = subprocess.run(["git", "-C", str(cwd), "-c", "core.quotePath=false", *args],
                                    input=stdin, capture_output=True, timeout=timeout, check=False,
                                    **({} if binary else {"encoding": "utf-8", "errors": "replace"}))
        except (OSError, subprocess.SubprocessError):
            self.errors += 1
            return None
        return None if result.returncode else result.stdout

    def run(self, cwd, *args, **kwargs):
        """One read-only git invocation; None when git is disabled, missing or fails."""
        return self._run(cwd, *args, **kwargs) if self.enabled else None

    def identity(self, root):
        """Repository identity shared by every worktree: the real common git directory."""
        if not root:
            return None
        if root not in self.identities:
            out = self.run(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
            self.identities[root] = os.path.realpath(out.strip()) if out and out.strip() else None
        return self.identities[root]

    def repo(self, cwd):
        if not self.enabled:
            return None
        candidate = self.override or cwd
        if not candidate:
            return None
        candidate = str(Path(candidate).expanduser())
        if candidate in self.repos:
            return self.repos[candidate]
        root = None
        try:
            exists = Path(candidate).is_dir()
        except (OSError, ValueError):  # recorded text can yield an unusable path
            exists = False
        if exists:
            out = self.run(candidate, "rev-parse", "--show-toplevel")
            if out and out.strip():
                root = out.strip()
        self.repos[candidate] = root
        return root

    def full(self, repo, sha):
        if len(sha) == 40:
            return sha
        out = self.run(repo, "rev-parse", "--verify", "--quiet", sha + "^{commit}")
        return out.strip() if out and re.fullmatch(SHA, out.strip()) else None

    def subject(self, repo, subject):
        subject = normalize_subject(subject)
        if not subject:
            return []
        key = (repo, subject)
        if key in self.subjects:
            return self.subjects[key]
        out = self.run(repo, "log", "--all", "--fixed-strings", "--grep", subject,
                        "--format=%H%x1f%s%x1f%ct", "-n", "50")
        rows = []
        for line in (out or "").splitlines():
            parts = line.split("\x1f")
            if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{40}", parts[0]):
                continue
            if normalize_subject(parts[1]) == subject:
                try:
                    rows.append((parts[0], parts[1], int(parts[2])))
                except ValueError:
                    pass
        self.subjects[key] = rows
        return rows
