"""Canned structural reports over one pinned graph: fan-in, orphans, cycles, untested files, largest units.

Every report is structure, not behavior: missing edges are candidates for reading, never proof.
"""
import collections
import re
from pathlib import PurePosixPath

from .graph_build import DEPENDENCIES

REPORTS = {
    "fanin": "Rank files (or symbols) by distinct consumers.",
    "orphans": "Code files (or callables) with no inbound edge; entrypoints and name mentions flagged.",
    "cycles": "File dependency cycles (strongly connected components).",
    "untested": "Code files with no TESTS edge; reports whether any test reaches them through imports.",
    "largest": "Largest callables/classes (or files) by lines.",
}
CALLABLES = ("function", "async_function", "method")
UNITS = CALLABLES + ("class", "struct", "enum", "impl", "trait")
# File names and folders that are run, not imported.
ENTRY_STEMS = {"main", "index", "cli", "app", "server", "__" + "main__", "__init__", "setup", "conftest", "build",
               "lib", "mod", "manage", "wsgi", "asgi"}
ENTRY_DIRS = {"bin", "scripts", "examples", "example", "tools", "migrations"}
MAIN = re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+main\s*\(|__name__\s*==\s*['\"]__ma[i]n__['\"]|^#!", re.M)
TRANSITIVE_DEPTH = 3


def code_files(graph, include_tests, keep=None):
    return {p: n for p, n in graph.files.items()
            if n.get("parse_state") in ("parsed", "parsed_with_errors") and (include_tests or not n.get("is_test"))
            and (keep is None or p in keep)}


def generated_files(graph):
    """Generated or vendored code files, largest share of nodes first."""
    counts = collections.Counter(n.get("path") for n in graph.nodes.values() if n["kind"] == "symbol")
    rows = [{"path": p, "reason": n["generated"], "symbols": counts[p]} for p, n in graph.files.items()
            if n.get("generated") and n.get("parse_state") in ("parsed", "parsed_with_errors")]
    return sorted(rows, key=lambda r: (-r["symbols"], r["path"]))


def text_of(graph):
    return {r["path"]: r["text"] for r in graph.body["source"]["files"] if r["text"] is not None}


def line_count(text):
    return text.count("\n") + int(bool(text) and not text.endswith("\n")) if text else 0


def file_edges(graph, predicates=DEPENDENCIES):
    """(source file, target file, edge) for dependency edges between distinct captured files."""
    for edge in graph.body["edges"]:
        if edge["predicate"] not in predicates:
            continue
        a, b = (graph.nodes[edge[k]].get("path") for k in ("source", "target"))
        if a in graph.files and b in graph.files and a != b:
            yield a, b, edge


def entrypoint(path, text):
    p = PurePosixPath(path)
    if p.stem in ENTRY_STEMS or p.stem.split(".")[0] in ENTRY_STEMS:
        return "entry_name"
    if set(p.parts[:-1]) & ENTRY_DIRS:
        return "entry_folder"
    if ".config." in p.name or p.name.endswith((".config.js", ".config.ts", ".config.mjs")):
        return "config_file"
    if text and MAIN.search(text):
        return "main_guard"
    return None


def fanin(graph, grain, include_tests, keep):
    if grain == "symbol":
        callers = collections.defaultdict(set)
        for edge in graph.body["edges"]:
            src = graph.nodes[edge["source"]]
            if (edge["predicate"] == "CALLS_STATIC" and graph.nodes[edge["target"]].get("path") in keep
                    and (include_tests or not (src.get("is_test") or graph.nodes[edge["target"]].get("is_test")))):
                callers[edge["target"]].add(edge["source"])
        rows = [{**graph.public(graph.nodes[t]), "callers": len(s),
                 "caller_files": len({graph.nodes[x].get("path") for x in s})} for t, s in callers.items()]
        return sorted(rows, key=lambda r: (-r["callers"], r["path"], r.get("line") or 0))
    files = code_files(graph, True, keep)
    consumers, tests, deps = (collections.defaultdict(set) for _ in range(3))
    for a, b, _ in file_edges(graph):
        deps[a].add(b)
        (tests if graph.files[a].get("is_test") else consumers)[b].add(a)
    rows = [{**graph.public(n), "consumers": len(consumers[p]), "test_consumers": len(tests[p]),
             "dependencies": len(deps[p])} for p, n in files.items()
            if include_tests or not n.get("is_test")]
    return sorted(rows, key=lambda r: (-r["consumers"], -r["test_consumers"], r["path"]))


def orphans(graph, grain, include_tests, keep):
    texts = text_of(graph)
    if grain == "symbol":
        called = {e["target"] for e in graph.body["edges"] if e["predicate"] == "CALLS_STATIC"}
        rows = []
        for n in graph.nodes.values():
            if (n["kind"] != "symbol" or n.get("symbol_kind") not in CALLABLES + ("class",) or n["id"] in called
                    or n.get("path") not in keep):
                continue
            if n.get("is_test") and not include_tests:
                continue
            mentions = graph.mentions(n["id"])
            rows.append({**graph.public(n), "lines": n["end_line"] - n["line"] + 1, "mentions": len(mentions),
                         "mention_examples": mentions[:3],
                         "state": "mentioned_without_edge" if mentions else "unreferenced",
                         "public_candidate": bool(n.get("public_candidate"))})
        # Unreferenced and private first: the strongest dead-code candidates.
        return sorted(rows, key=lambda r: (r["state"] != "unreferenced", r["public_candidate"], -r["lines"], r["path"]))
    files = code_files(graph, include_tests, keep)
    inbound, from_tests = collections.defaultdict(set), collections.defaultdict(set)
    for a, b, _ in file_edges(graph):
        (from_tests if graph.files[a].get("is_test") else inbound)[b].add(a)
    # Manifests and CI run files by path; a referenced file is an entrypoint, not dead code.
    manifests = collections.defaultdict(set)
    for a, b, _ in file_edges(graph, ("REFERENCES",)):
        manifests[b].add(a)
    words = graph.words()
    rows = []
    for path, n in files.items():
        if inbound[path]:
            continue
        stem = PurePosixPath(path).stem
        named = sorted({p for p, _ in words.get(stem, []) if p != path}) if len(stem) > 2 else []
        hint = "manifest_reference" if manifests[path] else entrypoint(path, texts.get(path))
        rows.append({**graph.public(n), "lines": line_count(texts.get(path)), "test_importers": len(from_tests[path]),
                     "entrypoint_hint": hint, "stem_mentioned_in": named[:5],
                     **({"referenced_by": sorted(manifests[path])[:5]} if manifests[path] else {}),
                     "state": "entrypoint_candidate" if hint else "test_only" if from_tests[path] else "no_inbound_edges"})
    order = {"no_inbound_edges": 0, "test_only": 1, "entrypoint_candidate": 2}
    return sorted(rows, key=lambda r: (order[r["state"]], -r["lines"], r["path"]))


def cycles(graph, include_tests, keep):
    files = code_files(graph, include_tests, keep)
    adjacency, witness = collections.defaultdict(set), {}
    for a, b, edge in file_edges(graph):
        if a in files and b in files:
            adjacency[a].add(b)
            witness.setdefault((a, b), edge)
    # Iterative Tarjan: deep import chains must not exhaust Python's stack.
    index, low, on, stack, found, counter = {}, {}, set(), [], [], [0]
    for root in sorted(files):
        if root in index:
            continue
        work = [(root, iter(sorted(adjacency[root])))]
        index[root] = low[root] = counter[0]; counter[0] += 1
        stack.append(root); on.add(root)
        while work:
            node, children = work[-1]
            child = next(children, None)
            if child is not None:
                if child not in index:
                    index[child] = low[child] = counter[0]; counter[0] += 1
                    stack.append(child); on.add(child)
                    work.append((child, iter(sorted(adjacency[child]))))
                elif child in on:
                    low[node] = min(low[node], index[child])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    item = stack.pop(); on.discard(item); component.append(item)
                    if item == node:
                        break
                if len(component) > 1:
                    found.append(sorted(component))
    rows = []
    for component in found:
        members = set(component)
        edges = [{"from": a, "to": b, "predicate": witness[(a, b)]["predicate"],
                  "line": (witness[(a, b)].get("evidence") or {}).get("line"),
                  "edge": f"{graph.snapshot}/edges/{witness[(a, b)]['id']}"}
                 for a in component for b in sorted(adjacency[a] & members)]
        rows.append({"kind": "cycle", "size": len(component), "files": component, "edges": edges[:20],
                     "edge_count": len(edges)})
    return sorted(rows, key=lambda r: (-r["size"], r["files"]))


def untested(graph, keep):
    texts = text_of(graph)
    # Rust `#[cfg(test)]` modules test their own file.
    files = {p: n for p, n in code_files(graph, False, keep).items() if not n.get("inline_tests")}
    direct = collections.Counter(b for a, b, _ in file_edges(graph, ("TESTS",)))
    forward = collections.defaultdict(set)
    for a, b, _ in file_edges(graph):
        forward[a].add(b)
    distance = {p: 0 for p, n in graph.files.items() if n.get("is_test")}
    frontier = list(distance)
    for step in range(1, TRANSITIVE_DEPTH + 1):
        frontier = [b for a in frontier for b in forward[a] if b not in distance]
        for b in frontier:
            distance.setdefault(b, step)
    rows = [{**graph.public(n), "lines": line_count(texts.get(p)), "test_distance": distance.get(p),
             "state": "reached_through_imports" if p in distance else "no_test_path"}
            for p, n in files.items() if not direct[p]]
    return sorted(rows, key=lambda r: (r["state"] != "no_test_path", -r["lines"], r["path"]))


def largest(graph, grain, include_tests, keep):
    if grain == "file":
        texts = text_of(graph)
        rows = [{**graph.public(n), "lines": line_count(texts.get(p)), "symbols": 0}
                for p, n in code_files(graph, include_tests, keep).items()]
        counts = collections.Counter(n.get("path") for n in graph.nodes.values() if n["kind"] == "symbol")
        for row in rows:
            row["symbols"] = counts[row["path"]]
    else:
        rows = [{**graph.public(n), "lines": n["end_line"] - n["line"] + 1} for n in graph.nodes.values()
                if n["kind"] == "symbol" and n.get("symbol_kind") in UNITS and n.get("path") in keep
                and (include_tests or not n.get("is_test"))]
    return sorted(rows, key=lambda r: (-r["lines"], r["path"], r.get("line") or 0))


def analyze(graph, report, grain=None, include_tests=False, exclude_entrypoints=False, exclude_public=False,
            include_generated=False):
    if grain == "symbol" and report in ("cycles", "untested"):
        raise ValueError(f"{report} supports only --grain file")
    grain = grain or ("symbol" if report == "largest" else "file")
    skipped = [] if include_generated else generated_files(graph)
    keep = set(graph.files) - {r["path"] for r in skipped}
    if report == "fanin":
        rows = fanin(graph, grain, include_tests, keep)
    elif report == "orphans":
        rows = orphans(graph, grain, include_tests, keep)
    elif report == "cycles":
        rows = cycles(graph, include_tests, keep)
    elif report == "untested":
        rows = untested(graph, keep)
    elif report == "largest":
        rows = largest(graph, grain, include_tests, keep)
    else:
        raise ValueError(f"Reports: {', '.join(REPORTS)}")
    texts = text_of(graph)
    if exclude_entrypoints:
        rows = [r for r in rows if not any(entrypoint(p, texts.get(p))
                for p in (r.get("files") or [r.get("path", "")]))]
    if exclude_public:
        if grain != "symbol":
            raise ValueError("--exclude-public requires --grain symbol")
        rows = [r for r in rows if not graph.nodes[r["id"]].get("public_candidate")]
    excluded = {"count": len(skipped), "files": skipped[:10], "include_with": "--include-generated"} if skipped else None
    inline = sorted(p for p, n in graph.files.items() if n.get("inline_tests")) if report == "untested" else []
    return graph.document("analyze", rows, excluded_generated=excluded,
                          **({"inline_tested": inline} if inline else {}), exclude_entrypoints=exclude_entrypoints, exclude_public=exclude_public, report=report, grain=grain if report in ("fanin", "orphans", "largest") else "file",
                          include_tests=include_tests, predicates=list(DEPENDENCIES),
                          assessment="structural candidates; read source before claiming dead, coupled or untested")
