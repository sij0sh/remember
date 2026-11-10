"""Retrieval features, seeds and the exact-needle probe (D127-D131). Internal; no public table.

A feature is a (kind, value) pair found in one text: repo-relative `path`, commit `sha`,
`error_exact` and `error_family`. Seeds are features the invocation already trusts (flags,
renames, symbol definitions, prior-work commits, the active request) and keep their
`seed_basis`. The probe searches raw recording bytes for exact seed needles only; word needles
over raw JSON are noise.
"""
import os
import re

from . import error_signatures
from .budget import check
from .graph_store import canonical_path

CHUNK = 1024 * 1024
MAX_SYMBOL_PATHS = 3
MAX_RENAME_COMMITS = 20
PRIOR_COMMITS, PRIOR_PATHS = 3, 5
# Seed bases whose matches rank as `exact`; the rest rank as `resolved` (D133).
EXACT_BASES = frozenset({"path", "error", "active_request", "prior_commit"})
_TOKEN = re.compile(r"[\w.~/@+-]+")
_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_SHA = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _path(token, root):
    token = token.strip("'\"()[]{}<>").rstrip(".,:;/")
    if "://" in token or not token or token.startswith("@"):
        return None
    if "/" not in token and not _EXT.search(token):
        return None
    if token.startswith(("/", "~")):
        if not root:
            return None
        expanded = os.path.expanduser(token)
        if expanded.startswith("~"):
            return None  # unknown user (~name): never a repository path
        full = canonical_path(expanded).replace(os.sep, "/")
        base = canonical_path(root).replace(os.sep, "/").rstrip("/") + "/"
        return full[len(base):] if full.startswith(base) and len(full) > len(base) else None
    token = token[2:] if token.startswith("./") else token
    return token if token and not token.startswith("../") else None


def features(text, root=None):
    """set[(kind, value)] of paths, SHAs and error signatures in one text."""
    text = str(text or "")
    out = set()
    for token in _TOKEN.findall(text):
        if ("/" in token or "." in token) and not _SHA.fullmatch(token):
            path = _path(token, root)
            if path:
                out.add(("path", path))
    out.update(("sha", sha) for sha in _SHA.findall(text.lower()))
    for exact, family in error_signatures.signature(text):
        out.add(("error_exact", exact))
        out.add(("error_family", family))
    return out


def seed(kind, value, basis):
    return {"kind": kind, "value": value, "seed_basis": basis}


def path_seeds(paths, resolver, root):
    """--path seeds plus their historical names (bounded rename follow)."""
    from .git_file_history import history
    out = [seed("path", p, "path") for p in paths]
    for path in paths if root else ():
        check()
        try:
            _, changes, _ = history(resolver, root, MAX_RENAME_COMMITS, follow=path)
        except (ValueError, OSError):
            continue
        for change in changes:
            for name in (change.get("old_path"), change.get("new_path"), change.get("path")):
                if name and name not in paths and name not in {s["value"] for s in out}:
                    out.append(seed("path", name, "rename"))
    return out


def symbol_seeds(goal, graph):
    """Goal identifiers equal to a symbol name -> defining files; more than three files is ambiguous."""
    if graph is None or not goal:
        return [], 0
    names = set(_IDENT.findall(goal))
    found = {}
    for node in graph.nodes.values():
        if node.get("kind") == "symbol" and node.get("name") in names:
            found.setdefault(node["name"], set()).add(node.get("path"))
    out, ambiguous = [], 0
    for name, paths in sorted(found.items()):
        if len(paths) > MAX_SYMBOL_PATHS:
            ambiguous += 1
            continue
        out += [seed("path", p, "symbol") for p in sorted(paths) if p]
    return out, ambiguous


def error_seeds(errors):
    out = []
    for text in errors:
        exact = " ".join(text.split())[:error_signatures.WIDTH]
        out += [seed("error_exact", exact, "error"), seed("error_family", error_signatures.family(exact), "error")]
    return out


def prior_seeds(prior):
    out = []
    for row in prior[:PRIOR_COMMITS]:
        out.append(seed("sha", row["sha"], "prior_commit"))
        out += [seed("path", p, "prior_commit_path") for p in (row.get("paths") or [])[:PRIOR_PATHS]]
    return out


def text_seeds(text, root):
    """Paths and errors (not words) from the active request's prose."""
    return [seed(kind, value, "active_request") for kind, value in sorted(features(text, root))
            if kind in ("path", "error_exact", "error_family")]


def unique(seeds):
    out, seen = [], set()
    for item in seeds:
        key = (item["kind"], item["value"])
        if item["value"] and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def needles(seeds):
    """Lowercased byte needles for the probe: paths, exact errors, SHAs of 7+ chars."""
    out = set()
    for item in seeds:
        value = item["value"]
        if item["kind"] in ("path", "error_exact") or (item["kind"] == "sha" and len(value) >= 7):
            # Raw JSON escapes quotes and backslashes; search the part that survives encoding unchanged.
            value = value.split('"')[0] if item["kind"] == "error_exact" else value
            if len(value) >= 4:
                out.add(value.lower().encode())
    return sorted(out)


def probe(files, needles_, budget):
    """Count distinct needles per file, newest first, within ``budget`` bytes.

    ``files`` = [(path, size, mtime_ns)]. Returns (hits {path: count}, coverage)."""
    hits, read, probed = {}, 0, 0
    overlap = max((len(n) for n in needles_), default=1) - 1
    ordered = sorted(files, key=lambda f: -f[2])
    for path, size, _ in ordered:
        if read >= budget:
            break
        check()
        found, tail = set(), b""
        try:
            with open(path, "rb") as stream:
                while read < budget:
                    chunk = stream.read(min(CHUNK, budget - read))
                    if not chunk:
                        break
                    read += len(chunk)
                    window = tail + chunk.lower()
                    found.update(n for n in needles_ if n not in found and n in window)
                    tail = window[-overlap:] if overlap else b""
        except OSError:
            continue
        probed += 1
        if found:
            hits[path] = len(found)
    return hits, {"files_probed": probed, "files_hit": len(hits), "bytes_probed": read,
                  "files_unprobed": len(ordered) - probed}
