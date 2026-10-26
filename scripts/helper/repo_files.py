"""Current worktree files and optional code structure, read-only and bounded.

Files are tracked paths plus nonignored untracked paths. A tracked path missing
from the worktree is kept with ``exists`` false. Path matches against recorded
activity show association only, never that today's content matches history.
"""
import ast
import datetime as dt
import hashlib
import os
import re
import stat
from pathlib import PurePosixPath

from . import code_symbols, import_targets

FILE_GRAIN = "one current worktree path"
ITEM_GRAIN = "one function, type, container or import binding in a parsed source file"
FILE_COLUMNS = {"repo_id": "VARCHAR", "worktree": "VARCHAR", "path": "VARCHAR", "dir": "VARCHAR",
                "name": "VARCHAR", "ext": "VARCHAR", "language": "VARCHAR", "file_type": "VARCHAR",
                "exists": "BOOLEAN", "size": "BIGINT", "lines": "BIGINT", "binary": "BOOLEAN",
                "encoding": "VARCHAR", "blob_sha": "VARCHAR", "tracked": "BOOLEAN",
                "index_status": "VARCHAR", "worktree_status": "VARCHAR", "staged": "BOOLEAN",
                "modified": "BOOLEAN", "read_state": "VARCHAR", "parse_state": "VARCHAR",
                "content": "VARCHAR", "content_omitted_chars": "BIGINT", "head_sha": "VARCHAR",
                "snapshot_at": "VARCHAR", "error": "VARCHAR"}
ITEM_COLUMNS = {"repo_id": "VARCHAR", "path": "VARCHAR", "kind": "VARCHAR", "name": "VARCHAR",
                "qualname": "VARCHAR", "parent": "VARCHAR", "module": "VARCHAR", "line": "BIGINT",
                "end_line": "BIGINT", "parser": "VARCHAR", "language": "VARCHAR", "node_kind": "VARCHAR",
                "target_path": "VARCHAR", "target_state": "VARCHAR", "target_rule": "VARCHAR"}
LANGUAGES = {".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
             ".jsx": "javascript", ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
             ".tsx": "typescript", ".go": "go", ".rs": "rust",
             ".java": "java", ".kt": "kotlin", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
             ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby", ".php": "php", ".swift": "swift",
             ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".sql": "sql", ".md": "markdown",
             ".rst": "rst", ".txt": "text", ".json": "json", ".jsonl": "jsonl", ".yaml": "yaml",
             ".yml": "yaml", ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".html": "html",
             ".css": "css", ".scss": "css", ".xml": "xml", ".csv": "csv", ".lua": "lua"}
# Interpreter token: (language, extension whose parser reads it, if any).
SHEBANGS = {"sh": ("shell", ".sh"), "bash": ("shell", ".sh"), "dash": ("shell", ".sh"), "zsh": ("shell", None),
            "python": ("python", None), "python3": ("python", None)}
MAX_CONTENT = 64 * 1024


def _status(resolver, worktree):
    """{path: (index_status, worktree_status)} from porcelain v2; untracked paths use '?'."""
    out = resolver.run(worktree, "status", "--porcelain=v2", "-z", "--untracked-files=all") or ""
    found, tokens, i = {}, out.split("\0"), 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("1 "):
            parts = token.split(" ", 8)
            found[parts[8]] = (parts[1][0], parts[1][1])
        elif token.startswith("2 "):
            parts = token.split(" ", 9)
            found[parts[9]] = (parts[1][0], parts[1][1])
            i += 1  # the rename source path follows
        elif token.startswith("u "):
            parts = token.split(" ", 10)
            found[parts[10]] = ("U", "U")
        elif token.startswith("? "):
            found[token[2:]] = ("?", "?")
        i += 1
    return found


def _shebang(data):
    """(language, parse extension) from an extensionless file's `#!` line; never executed."""
    line = data[:256].split(b"\n", 1)[0].decode("utf-8", "replace")
    if not line.startswith("#!"):
        return None
    return next((SHEBANGS[token] for token in re.split(r"[^A-Za-z0-9]+", line[2:]) if token in SHEBANGS), None)


def _symbols(text, repo_id, path):
    """Definitions and imports from Python's ast; nested names are dotted qualnames."""
    tree = ast.parse(text, filename=path)
    rows = []

    def visit(node, parents):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = ("class" if isinstance(child, ast.ClassDef)
                        else "method" if parents and parents[-1][1] == "class"
                        else "async_function" if isinstance(child, ast.AsyncFunctionDef) else "function")
                names = [name for name, _ in parents] + [child.name]
                rows.append({"repo_id": repo_id, "path": path, "kind": kind, "name": child.name,
                             "qualname": ".".join(names), "parent": ".".join(names[:-1]) or None,
                             "module": None, "line": child.lineno, "end_line": child.end_lineno,
                             "parser": "python-ast", "language": "python",
                             "node_kind": type(child).__name__})
                visit(child, parents + [(child.name, "class" if kind == "class" else "function")])
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                module = child.module if isinstance(child, ast.ImportFrom) else None
                if isinstance(child, ast.ImportFrom) and child.level:
                    module = "." * child.level + (module or "")
                for alias in child.names:
                    rows.append({"repo_id": repo_id, "path": path, "kind": "import",
                                 "name": alias.asname or alias.name,
                                 "qualname": (module + alias.name if module.endswith(".") else f"{module}.{alias.name}")
                                             if module else alias.name,
                                 "parent": ".".join(name for name, _ in parents) or None,
                                 "module": module or alias.name, "line": child.lineno,
                                 "end_line": child.end_lineno, "parser": "python-ast",
                                 "language": "python", "node_kind": type(child).__name__})
            else:
                visit(child, parents)

    visit(tree, [])
    return rows


def parse_symbols(data, text, repo_id, path, language, parse_ext):
    """(rows, parse_state, error) for one file's content; never raises for bad source."""
    if code_symbols.supported(parse_ext):
        try:
            found, error = code_symbols.symbols(data, repo_id, path, parse_ext)
        except code_symbols.ParserUnavailable as error:
            return [], "parser_unavailable", str(error)[:300]
        return found, "parsed_with_errors" if error else "parsed", error
    if language != "python":
        return [], "unsupported", None
    try:
        return _symbols(text, repo_id, path), "parsed", None
    except (SyntaxError, ValueError, RecursionError) as error:
        return [], "syntax_error", str(error)[:300]


def language_of(path, data):
    """(language, parse extension) from the extension, or from a shebang when there is none."""
    ext = PurePosixPath(path).suffix.lower()
    if ext:
        return LANGUAGES.get(ext), ext
    return _shebang(data) or (None, None)


def snapshot(resolver, worktree, max_files, max_file_bytes, content=False, symbols=False):
    """(files, code_items, coverage) for one worktree; reads at most max_file_bytes per file."""
    repo_id = resolver.identity(worktree)
    listed = resolver.run(worktree, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                          "--deduplicate", timeout=60)
    if listed is None:
        return [], [], {"complete": False, "error": "git ls-files failed (not a repository)"}
    tracked_out = resolver.run(worktree, "ls-files", "-z", "--cached", "--deduplicate", timeout=60) or ""
    tracked = set(filter(None, tracked_out.split("\0")))
    paths = sorted(set(filter(None, listed.split("\0"))))
    statuses = _status(resolver, worktree)
    head = (resolver.run(worktree, "rev-parse", "--verify", "--quiet", "HEAD") or "").strip() or None
    at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    coverage = {"complete": len(paths) <= max_files, "paths_listed": len(paths), "files_limit": max_files,
                "max_file_bytes": max_file_bytes, "head_sha": head, "snapshot_at": at,
                "read_errors": 0, "parse_errors": 0, "too_large": 0, "parsers_unavailable": []}
    files, items = [], []
    for path in paths[:max_files]:
        pure = PurePosixPath(path)
        index_status, worktree_status = statuses.get(path, (".", "."))
        row = {"repo_id": repo_id, "worktree": worktree, "path": path,
               "dir": str(pure.parent) if str(pure.parent) != "." else "", "name": pure.name,
               "ext": pure.suffix.lower() or None, "language": LANGUAGES.get(pure.suffix.lower()),
               "tracked": path in tracked, "index_status": index_status, "worktree_status": worktree_status,
               "staged": index_status not in (".", "?"), "modified": worktree_status not in (".", "?"),
               "head_sha": head, "snapshot_at": at, "parse_state": "not_requested"}
        full = os.path.join(worktree, path)
        try:
            info = os.lstat(full)
        except OSError:
            row.update(exists=False, file_type="missing", read_state="missing")
            files.append(row)
            continue
        row["exists"], row["size"] = True, info.st_size
        if stat.S_ISLNK(info.st_mode):
            row.update(file_type="symlink", read_state="not_read")
        elif stat.S_ISDIR(info.st_mode):
            row.update(file_type="submodule", read_state="not_read")
        elif not stat.S_ISREG(info.st_mode):
            row.update(file_type="other", read_state="not_read")
        elif info.st_size > max_file_bytes:
            row.update(file_type="file", read_state="too_large")
            coverage["too_large"] += 1
        else:
            row["file_type"] = "file"
            try:
                with open(full, "rb") as stream:
                    data = stream.read(max_file_bytes + 1)
            except OSError as error:
                row.update(read_state="error", error=str(error)[:300])
                coverage["read_errors"] += 1
                files.append(row)
                continue
            # The same object id git would store for this worktree content.
            row["blob_sha"] = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
            row["binary"] = b"\0" in data[:8192]
            row["read_state"] = "read"
            if not row["binary"]:
                row["language"], parse_ext = language_of(path, data)
                try:
                    text = data.decode("utf-8")
                    row["encoding"] = "utf-8"
                except UnicodeDecodeError:
                    text = data.decode("latin-1")
                    row["encoding"] = "unknown"
                row["lines"] = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
                if content:
                    row["content"] = text[:MAX_CONTENT]
                    row["content_omitted_chars"] = max(0, len(text) - MAX_CONTENT)
                if symbols:
                    found, row["parse_state"], error = parse_symbols(data, text, repo_id, path, row["language"],
                                                                    parse_ext)
                    items.extend(found)
                    if error:
                        row["error"] = error
                    if row["parse_state"] == "syntax_error":
                        coverage["parse_errors"] += 1
                    elif (row["parse_state"] == "parser_unavailable"
                          and row["language"] not in coverage["parsers_unavailable"]):
                        coverage["parsers_unavailable"].append(row["language"])
            elif symbols:
                row["parse_state"] = "unsupported"
        files.append(row)
    if symbols:
        def read(path):
            try:
                with open(os.path.join(worktree, path), "rb") as stream:
                    return stream.read(import_targets.MAX_MANIFEST).decode("utf-8", "replace")
            except OSError:
                return None
        coverage["import_targets"] = import_targets.annotate(items, paths, read, worktree)
    return files, items, coverage
