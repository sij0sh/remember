"""Bounded source capture and immutable local evidence. No project code is executed."""
import fnmatch
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import stat
import subprocess
import tempfile
from pathlib import Path

SCHEMA = 1
DEFAULT_EXCLUDES = (".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
                    ".remember", ".pytest_cache", "target", "dist", "build")
ID = re.compile(r"^(snapshot|report):[0-9a-f]{64}$")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def git(root, *args):
    try:
        p = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args],
                           capture_output=True, timeout=30)
        return p.stdout.decode("utf-8", "surrogateescape") if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def exclusions(root, extra):
    rules = list(DEFAULT_EXCLUDES) + list(extra)
    path = root / ".rememberignore"
    if path.is_file() and not path.is_symlink():
        if path.stat().st_size > 65536:
            raise ValueError(".rememberignore exceeds 64 KiB")
        rules += [s.strip() for s in path.read_text(encoding="utf-8").splitlines() if s.strip() and not s.lstrip().startswith("#")]
    if any(r.startswith("!") for r in rules):
        raise ValueError("Exclusions are globs; negation (!) is not supported")
    return sorted(set(r.rstrip("/") for r in rules if r))


def excluded(path, rules):
    parts = path.split("/")
    prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    return next((r for r in rules if (any(fnmatch.fnmatchcase(p, r) for p in prefixes)
                 if "/" in r else any(fnmatch.fnmatchcase(p, r) for p in parts))), None)


def inventory(root, rules, max_files):
    """Git ignore rules when available; explicit glob exclusions in either mode."""
    raw = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--deduplicate")
    paths, omissions, complete = [], [], True
    mode = "git" if raw is not None else "directory"
    if raw is not None:
        candidates = sorted(set(p for p in raw.split("\0") if p))
        for p in candidates:
            if p.startswith("/") or ".." in Path(p).parts:
                omissions.append({"path": p, "reason": "outside_root"})
            elif excluded(p, rules):
                omissions.append({"path": p, "reason": "excluded"})
            else:
                paths.append(p)
        complete = len(paths) <= max_files
        paths = paths[:max_files]
    else:
        def onerror(error):
            nonlocal complete
            complete = False
            omissions.append({"path": str(error.filename), "reason": "inventory_error"})
        for folder, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
            rel = Path(folder).relative_to(root)
            keep = []
            for name in sorted(dirs):
                path = (rel / name).as_posix()
                if excluded(path, rules):
                    omissions.append({"path": path, "reason": "excluded"})
                elif (root / path).is_symlink():
                    names.append(name)
                else:
                    keep.append(name)
            dirs[:] = keep
            for name in sorted(names):
                path = (rel / name).as_posix()
                if excluded(path, rules):
                    omissions.append({"path": path, "reason": "excluded"})
                    continue
                if len(paths) == max_files:
                    complete = False
                    dirs[:] = []
                    break
                paths.append(path)
            if len(paths) == max_files and not complete:
                break
    return sorted(paths), {"mode": mode, "complete": complete, "exclusions": omissions}


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)


def read_file(root, path, max_bytes, remaining):
    row = {"path": path, "digest": None, "text": None, "state": "unread", "size": None}
    full = root / path
    try:
        # Never follow a directory symlink even for paths reported by Git.
        if any(parent.is_symlink() for parent in full.parents if parent != root and root in parent.parents):
            row["state"] = "symlink_parent"
            return row, None
        before = full.lstat()
        row["size"] = before.st_size
        if stat.S_ISLNK(before.st_mode):
            row.update(state="symlink", digest=hashlib.sha256(os.readlink(full).encode()).hexdigest())
            return row, signature(before)
        if not stat.S_ISREG(before.st_mode):
            row["state"] = "non_regular"
            return row, signature(before)
        if before.st_size > max_bytes or before.st_size > remaining:
            row["state"] = "file_limit" if before.st_size > max_bytes else "byte_limit"
            return row, signature(before)
        fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(min(max_bytes, remaining) + 1)
            after = os.fstat(stream.fileno())
        row["bytes_read"] = len(data)
        if signature(before) != signature(after) or len(data) > min(max_bytes, remaining):
            row["state"] = "changed_during_capture"
        else:
            row["digest"] = hashlib.sha256(data).hexdigest()
            try:
                row["text"] = data.decode("utf-8") if b"\0" not in data else None
            except UnicodeDecodeError:
                pass
            row["state"] = "read" if row["text"] is not None else "binary_or_non_utf8"
        return row, signature(after)
    except FileNotFoundError:
        row["state"] = "missing"
    except OSError as error:
        row.update(state="read_error", error=str(error))
    return row, None


def capture(root, *, max_files=20000, max_file_bytes=1024*1024, max_total_bytes=32*1024*1024, exclude=()):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Repository directory does not exist: {root}")
    rules = exclusions(root, exclude)
    paths, coverage = inventory(root, rules, max_files)
    files, signatures, used = [], {}, 0
    for path in paths:
        row, sig = read_file(root, path, max_file_bytes, max_total_bytes - used)
        used += row.get("bytes_read", 0)
        files.append(row)
        signatures[path] = sig
    again, later = inventory(root, rules, max_files)
    changed = paths != again or coverage != later
    for row in files:
        try:
            sig = signature((root / row["path"]).lstat())
        except OSError:
            sig = None
        if sig != signatures[row["path"]]:
            row.update(state="changed_during_capture", text=None, digest=None)
            changed = True
    coverage.update(stable=not changed, files=len(files), bytes_read=used,
                    limits={"max_files": max_files, "max_file_bytes": max_file_bytes, "max_total_bytes": max_total_bytes})
    return {"root": str(root), "files": files, "inventory": coverage,
            "exclusion_rules": rules, "extra_exclusions": list(exclude),
            "git": {"available": coverage["mode"] == "git",
                    "head": (git(root, "rev-parse", "--verify", "HEAD") or "").strip() or None,
                    "status": git(root, "status", "--porcelain=v1", "--untracked-files=all", "--", ".")}}


def producer_identity():
    base = Path(__file__).parent
    sources = {name: hashlib.sha256((base / name).read_bytes()).hexdigest() for name in
               ("graph_build.py", "graph_store.py", "repo_files.py", "code_symbols.py", "import_targets.py",
                "syntax_calls.py")}
    packages = {}
    for name in ("tree-sitter", "tree-sitter-bash", "tree-sitter-go", "tree-sitter-javascript",
                 "tree-sitter-rust", "tree-sitter-typescript"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {"schema": SCHEMA, "python": platform.python_version(), "sources": sources, "packages": packages}


def cache_home():
    """$XDG_CACHE_HOME, else %LOCALAPPDATA% on Windows, else ~/.cache."""
    base = os.environ.get("XDG_CACHE_HOME") or (os.environ.get("LOCALAPPDATA") if os.name == "nt" else None)
    return Path(base) if base else Path.home()/".cache"


class Store:
    def __init__(self, root, cache=None):
        self.root = str(Path(root).expanduser().resolve())
        base = Path(cache).expanduser() if cache else cache_home()/"remember"
        # Case-insensitive Windows paths name one workspace however they are spelled.
        self.path = base.resolve() / digest(os.path.normcase(self.root))
        # Cache inside the input tree would contaminate future captures.
        if Path(self.root) == self.path or Path(self.root) in self.path.parents:
            raise ValueError("Use a cache directory outside the repository")

    def save(self, kind, body):
        key = f"{kind}:{digest(body)}"
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest = self.path / (key.replace(":", "-") + ".json")
        if dest.exists():
            self.load(key)
            return key
        fd, name = tempfile.mkstemp(dir=self.path, prefix="pending-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical(body))
            os.replace(name, dest)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return key

    def load(self, key):
        if not ID.fullmatch(key):
            raise ValueError("Invalid retained reference; use a returned snapshot or report ID")
        path = self.path / (key.replace(":", "-") + ".json")
        try:
            body = json.loads(path.read_bytes())
        except FileNotFoundError:
            raise ValueError(f"Reference unavailable in this repository/cache: {key}") from None
        except (ValueError, OSError):
            raise ValueError(f"Corrupt retained reference: {key}") from None
        if digest(body) != key.split(":", 1)[1]:
            raise ValueError(f"Retained reference failed integrity check: {key}")
        return body

    def snapshot(self, **options):
        from .graph_build import build
        source = capture(self.root, **options)
        identity = producer_identity()
        # A small source-key index avoids re-parsing an unchanged captured tree.
        lookup = self.path / ("source-" + digest({"source": source, "producer": identity}) + ".json")
        if lookup.is_file():
            try:
                key = json.loads(lookup.read_bytes())["snapshot"]
                body = self.load(key)
                if body["source"] == source and body["producer"] == identity:
                    return key, body
            except (ValueError, KeyError, OSError):
                pass
        body = {"schema": SCHEMA, "source": source, "producer": identity, **build(source)}
        key = self.save("snapshot", body)
        fd, temp = tempfile.mkstemp(dir=self.path, prefix="lookup-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"snapshot": key}, stream)
            os.replace(temp, lookup)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return key, body

    def freshness(self, body):
        before = body["source"]
        now = capture(self.root, **before["inventory"]["limits"], exclude=before["extra_exclusions"])
        fields = lambda src: {f["path"]: (f["digest"], f["state"]) for f in src["files"]}
        old, new = fields(before), fields(now)
        changed = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
        reliable = all(s["inventory"]["complete"] and s["inventory"]["stable"]
                       and all(f["digest"] is not None or f["state"] == "missing" for f in s["files"])
                       for s in (before, now))
        return {"state": "changed" if changed or before["exclusion_rules"] != now["exclusion_rules"]
                else "unchanged_in_scope" if reliable else "unknown", "changed_paths": changed,
                "producer_changed": body["producer"] != producer_identity(),
                "scope": "captured inventory and contents; excluded paths are outside scope"}
