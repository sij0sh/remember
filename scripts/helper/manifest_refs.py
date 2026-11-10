"""Literal path references in build manifests and CI files; light parsing, never execution.

Read: package.json scripts/main/module/types/bin/exports/files, tsconfig
extends/include/files/references, Cargo.toml target paths, path dependencies
and workspace members, and CI workflow lines. A reference is
``(line, field, raw text, repo-relative path, strong)``. ``strong`` marks a
token that is certainly a path, so a missing target is worth reporting; bare
words in scripts count only when they name a captured path.
"""
import posixpath
import re
import shlex

from .import_targets import _jsonc

CI = re.compile(r"^(?:\.github/workflows/[^/]+\.ya?ml|\.gitlab-ci\.ya?ml|\.circleci/config\.ya?ml)$")
TSCONFIG = re.compile(r"^(?:tsconfig(?:\.[\w-]+)?|jsconfig)\.json$")
EXTENSION = re.compile(r"\.(?:[cm]?[jt]sx?|py|sh|bash|rs|go|json|jsonc|toml|ya?ml|d\.ts)$")
PATHLIKE = re.compile(r"^(?:\.{1,2}/)?[\w.-]+(?:/[\w.*-]+)+/?$")
# Output locations are written by the command, so a missing one is expected.
OUTPUT_FLAGS = {"-o", "--out", "--outdir", "--outfile", "--out-dir", "--out-file", "--outDir", "--output",
                ">", ">>", "--dist-dir"}
# Build output folders; a reference into one names an artifact, not a missing input.
OUTPUT_DIRS = {"dist", "build", "out", "target", "node_modules", "coverage", ".next"}
SKIP = ("node_modules/", "http:", "https:", "@", "$", "~", "/", "-")
GLOB = re.compile(r"[*?[{]")


def _norm(base, value):
    joined = posixpath.normpath(posixpath.join(base, value))
    return None if joined.startswith("../") or joined == ".." else ("" if joined == "." else joined)


def _line(text, needle, start=0):
    """1-based line of the first quoted occurrence at or after line ``start``, else the first bare one."""
    lines = text.splitlines()
    for wanted in (f'"{needle}"', needle):
        for number in range(start, len(lines)):
            if wanted in lines[number]:
                return number + 1
    return None


def _static(value):
    """The part of a glob before its first wildcard component, without a trailing slash."""
    parts = []
    for part in value.split("/"):
        if GLOB.search(part):
            break
        parts.append(part)
    return "/".join(parts).rstrip("/")


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _tokens(command):
    try:
        words = shlex.split(command, comments=False)
    except ValueError:
        words = command.split()
    output = False
    for word in words:
        if word in OUTPUT_FLAGS:
            output = True
            continue
        flag, sep, value = word.partition("=")
        if sep and flag.startswith("-"):
            yield value, flag in OUTPUT_FLAGS
        elif not word.startswith("-") and word not in ("&&", "||", ";", "|"):
            yield word, output
        output = False


def _command_refs(text, base, field, command, start=0):
    line = _line(text, command[:60], start) if command else None
    for token, output in _tokens(command):
        token = token.strip("'\"").rstrip(";,)")
        if not token or token.startswith(SKIP) or "://" in token or "$" in token:
            continue
        strong = bool(PATHLIKE.match(token) or (EXTENSION.search(token) and token.count("/") == 0
                                                  and re.match(r"^[\w.-]+$", token)))
        path = _norm(base, _static(token))
        if path is not None:
            yield line, field, token, path, strong and not output


def package_json(path, text):
    body = _jsonc(text) or {}
    base = posixpath.dirname(path)
    for name, command in (body.get("scripts") or {}).items():
        if isinstance(command, str):
            yield from _command_refs(text, base, f"scripts.{name}", command)
    for field in ("main", "module", "types", "typings", "bin", "exports", "files", "browser"):
        for value in _strings(body.get(field)):
            if field == "files" or value.startswith(("./", "../")) or "/" in value or EXTENSION.search(value):
                target = _norm(base, _static(value))
                if target is not None:
                    # Build outputs are named here before they exist; only files/types inputs are strong.
                    yield _line(text, value), field, value, target, False


def tsconfig(path, text):
    body = _jsonc(text) or {}
    base = posixpath.dirname(path)
    extends = body.get("extends")
    for value in _strings(extends):
        if value.startswith(("./", "../")):
            target = _norm(base, value)
            if target is not None:
                yield _line(text, value), "extends", value, target, True
    for field in ("include", "files"):
        for value in _strings(body.get(field)):
            target = _norm(base, _static(value))
            if target is not None:
                yield _line(text, value), field, value, target, True
    for item in body.get("references") or []:
        value = item.get("path") if isinstance(item, dict) else None
        if isinstance(value, str):
            target = _norm(base, value)
            if target is not None:
                yield _line(text, value), "references", value, target, True


def cargo(path, text):
    try:
        import tomllib
        body = tomllib.loads(text)
    except Exception:
        return
    base = posixpath.dirname(path)
    found = []
    lib = body.get("lib")
    if isinstance(lib, dict) and isinstance(lib.get("path"), str):
        found.append(("lib.path", lib["path"]))
    for table in ("bin", "test", "bench", "example"):
        for item in body.get(table) or []:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                found.append((f"{table}.path", item["path"]))
    package = body.get("package")
    if isinstance(package, dict) and isinstance(package.get("build"), str):
        found.append(("package.build", package["build"]))
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        for name, spec in (body.get(section) or {}).items():
            if isinstance(spec, dict) and isinstance(spec.get("path"), str):
                found.append((f"{section}.{name}", spec["path"]))
    workspace = body.get("workspace")
    if isinstance(workspace, dict):
        found += [("workspace.members", m) for m in workspace.get("members") or [] if isinstance(m, str)]
    for field, value in found:
        target = _norm(base, _static(value))
        if target is not None:
            yield _line(text, value), field, value, target, True


def ci(path, text):
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith(("#", "uses:", "- uses:", "image:")) or "uses:" in stripped:
            continue
        for token, output in _tokens(stripped.split(":", 1)[1] if re.match(r"^-?\s*[\w-]+:\s", stripped) else stripped):
            token = token.strip("'\"").rstrip(";,)")
            if (not token or token.startswith(SKIP) or "://" in token or "$" in token
                    or not (token.startswith("./") or ("/" in token and EXTENSION.search(token)))):
                continue
            target = _norm("", _static(token))
            if target is not None:
                yield number, "run", token, target, not output


def references(path, text):
    """Literal path references in one manifest or CI file; empty for every other file."""
    name = posixpath.basename(path)
    if name == "package.json":
        reader = package_json
    elif TSCONFIG.match(name):
        reader = tsconfig
    elif name == "Cargo.toml":
        reader = cargo
    elif CI.match(path):
        reader = ci
    else:
        return []
    seen, out = set(), []
    for line, field, raw, target, strong in reader(path, text or ""):
        if target and target != path and (field, target) not in seen:
            seen.add((field, target))
            strong = strong and not OUTPUT_DIRS & set(target.split("/"))
            out.append({"line": line or 1, "field": field, "text": raw, "target": target, "strong": strong})
    return out
