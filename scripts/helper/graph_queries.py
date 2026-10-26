"""Typed traversal, change impact and read-only SQL over one pinned source graph."""
import collections
import fnmatch
import json
import math
import os
import re
import sqlite3
import time
from pathlib import PurePosixPath

from .graph_build import DEPENDENCIES, is_test


def reference(snapshot, section, key):
    return f"{snapshot}/{section}/{key}"


class Graph:
    def __init__(self, snapshot, body):
        self.snapshot, self.body = snapshot, body
        self.nodes = {n["id"]: n for n in body["nodes"]}
        self.files = {n["path"]: n for n in body["nodes"] if n["kind"] == "file"}
        self._words = None

    def public(self, node):
        return {**node, "ref": reference(self.snapshot, "nodes", node["id"])}

    def resolve(self, target):
        target = target.removeprefix("./")
        # A returned node ref names its node directly.
        _, marker, ident = target.rpartition("/nodes/")
        if marker and ident in self.nodes:
            return [ident]
        if target in self.nodes:
            return [target]
        exact = [n["id"] for n in self.nodes.values() if n["key"] == target
                 or (n["kind"] in ("file", "directory") and n.get("path") == target)
                 or (n["kind"] == "symbol" and f"{n['path']}::{n['qualname']}" == target)]
        if exact:
            return exact
        named = [n["id"] for n in self.nodes.values() if n.get("qualname") == target or n.get("name") == target]
        if named:
            return named
        # A path tail ("lib.rs", "lib.rs::Type::method") matches on whole path components.
        tail = "/" + target
        return [n["id"] for n in self.nodes.values()
                if (n["kind"] in ("file", "directory") and ("/" + (n.get("path") or "")).endswith(tail))
                or (n["kind"] == "symbol" and f"/{n['path']}::{n['qualname']}".endswith(tail))]

    def unique(self, target):
        hits = self.resolve(target)
        if len(hits) != 1:
            raise ValueError(json.dumps({"target": target, "resolution": "ambiguous" if hits else "unresolved",
                                         "candidates": [self.public(self.nodes[n]) for n in hits[:20]]}))
        return hits[0]

    def project(self, ident, grain):
        n = self.nodes[ident]
        if grain == "file" and n["kind"] == "symbol":
            return self.files[n["path"]]["id"]
        return ident

    def seeds(self, target, grain):
        ident = self.unique(target)
        node = self.nodes[ident]
        if node["kind"] == "directory":
            prefix = "" if node["path"] == "." else node["path"] + "/"
            return {n["id"] for n in self.files.values() if n["path"].startswith(prefix)}
        return {self.project(ident, grain)}

    def adjacency(self, direction, predicates, grain, impact=False):
        adjacency = collections.defaultdict(list)
        for edge in self.body["edges"]:
            if edge["predicate"] not in predicates:
                continue
            a, b = (self.project(edge[k], grain) for k in ("source", "target"))
            if a == b and grain == "file":
                continue
            if impact and any(self.nodes[n].get("is_test") for n in (a, b)):
                continue
            if direction in ("out", "both"):
                adjacency[a].append((b, edge))
            if direction in ("in", "both"):
                adjacency[b].append((a, edge))
        for values in adjacency.values():
            values.sort(key=lambda item: (item[0], item[1]["id"]))
        return adjacency

    def walk(self, seeds, direction="in", predicates=DEPENDENCIES, grain="file", depth=2, max_nodes=1000,
             impact=False, seconds=5.0):
        start = time.monotonic()
        adjacency = self.adjacency(direction, predicates, grain, impact)
        seeds = set(seeds)
        selected = sorted(seeds)[:max_nodes]
        reached = {n: (0, []) for n in selected}
        queue, frontier = collections.deque(selected), []
        bounds = set()
        if len(selected) < len(seeds):
            bounds.add("nodes")
            frontier.extend(sorted(seeds - set(selected)))
        while queue:
            source = queue.popleft()
            distance, trail = reached[source]
            # Tests can be selected/returned but never bridge unrelated implementations.
            if self.nodes[source].get("is_test") and source not in seeds:
                continue
            for target, edge in adjacency[source]:
                if time.monotonic() - start > seconds:
                    bounds.add("time")
                    frontier.extend([source, *queue])
                    queue.clear()
                    break
                if target in reached:
                    continue
                if distance >= depth:
                    bounds.add("depth")
                    frontier.append(target)
                    continue
                if len(reached) >= max_nodes:
                    bounds.add("nodes")
                    frontier.append(target)
                    continue
                step = {"from": source, "to": target, "edge": reference(self.snapshot, "edges", edge["id"]),
                        "predicate": edge["predicate"], "resolution": edge["resolution"]}
                reached[target] = (distance + 1, trail + [step])
                queue.append(target)
        results = [{**self.public(self.nodes[n]), "distance": distance,
                    "via": trail[-1]["predicate"] if trail else None, "witness": trail}
                   for n, (distance, trail) in sorted(reached.items(), key=lambda p: (p[1][0], self.nodes[p[0]]["key"]))
                   if n not in seeds]
        return results, {"complete_within_predicates": not bounds, "bounds": sorted(bounds),
                         "depth": depth, "max_nodes": max_nodes, "seconds": seconds,
                         "frontier": [self.public(self.nodes[n]) for n in sorted(set(frontier))]}

    def within(self, target):
        """Paths a map target covers: a file, a directory's files, or a symbol's file."""
        return {self.nodes[n]["path"] for n in self.seeds(target, "file")}

    def find(self, text, word=False, case_sensitive=False, regex=False):
        try:
            pattern = re.compile((rf"\b(?:{text})\b" if word else text) if regex
                                 else (rf"\b{re.escape(text)}\b" if word else re.escape(text)),
                                 0 if case_sensitive else re.IGNORECASE)
        except re.error as error:
            raise ValueError(f"Invalid --regex pattern: {error}") from None
        results = []
        for node in self.nodes.values():
            fields = [f for f in ("name", "key", "qualname", "path") if pattern.search(str(node.get(f) or ""))]
            if fields:
                results.append({**self.public(node), "match": fields})
        for row in self.body["source"]["files"]:
            if row["text"] is None:
                continue
            for number, line in enumerate(row["text"].splitlines(), 1):
                if pattern.search(line):
                    results.append({"kind": "occurrence", "path": row["path"], "line": number,
                                    "text": line, "ref": reference(self.snapshot, "source", row["path"]),
                                    "basis": "literal_match"})
        return sorted(results, key=lambda r: (r.get("path") or "", r.get("line") or 0, r["kind"]))

    def words(self):
        """Identifier -> [(path, line)] over parsed code, import lines excluded; built once per graph."""
        if self._words is None:
            self._words = collections.defaultdict(list)
            # Import statements name symbols without using them.
            imports = {(e["evidence"]["path"], line) for e in self.body["edges"]
                       if e["predicate"] == "IMPORTS" and e.get("evidence")
                       for line in range(e["evidence"]["line"], e["evidence"]["end_line"] + 1)}
            for row in self.body["source"]["files"]:
                node = self.files.get(row["path"], {})
                if row["text"] is None or node.get("parse_state") not in ("parsed", "parsed_with_errors"):
                    continue
                for number, line in enumerate(row["text"].splitlines(), 1):
                    if (row["path"], number) in imports:
                        continue
                    for word in set(WORD.findall(line)):
                        self._words[word].append((row["path"], number))
        return self._words

    def mentions(self, ident):
        """Source lines naming a symbol outside its declaration and outside edges that reach it."""
        node = self.nodes[ident]
        if node["kind"] != "symbol":
            return []
        covered = {(e["evidence"]["path"], line) for e in self.body["edges"]
                   if e["target"] == ident and e["predicate"] != "DECLARES" and e.get("evidence")
                   for line in range(e["evidence"]["line"], e["evidence"]["end_line"] + 1)}
        return [{"path": path, "line": line} for path, line in self.words().get(node["name"], [])
                if (path, line) not in covered
                and not (path == node["path"] and node["line"] <= line <= node["end_line"])]

    def document(self, operation, results, **scope):
        return {"operation": operation, "snapshots": [self.snapshot], "results": results,
                "coverage": self.body["coverage"], "gaps": self.body["gaps"], "scope": scope}


WORD = re.compile(r"[A-Za-z_$][\w$]*")


def path_matches(path, globs):
    """A glob matches with fnmatch (`*` crosses `/`); a plain path matches itself and everything under it."""
    if not path:
        return False
    for glob in globs:
        if any(c in glob for c in "*?["):
            if fnmatch.fnmatchcase(path, glob):
                return True
        elif path == glob.rstrip("/") or path.startswith(glob.rstrip("/") + "/"):
            return True
    return False


def unresolved_mentions(graph, target, exclude_paths=()):
    """A gap when source names a symbol target where the graph holds no edge: empty results mean unknown."""
    hits = graph.resolve(target)
    if len(hits) != 1:
        return None
    rows = [m for m in graph.mentions(hits[0]) if m["path"] not in exclude_paths]
    if not rows:
        return None
    return {"area": "mentions", "reason": "unresolved_mentions", "target": target, "count": len(rows),
            "examples": rows[:10], "meaning": "Source names this symbol where the graph has no edge; "
            "read these lines before treating a short or empty result as complete."}


# One file above this share of nodes skews fan-in, size and node counts.
DOMINANT_SHARE = 0.25
# Disk directories that never hold project source; references to them are expected.
UNREFERENCED = {".git", ".hg", ".svn", ".remember", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache"}
# Prose and ignore lists name directories without using them.
QUIET = (".md", ".markdown", ".rst", ".txt", ".adoc", "ignore")


def warnings(graph, root):
    """Capture skews an agent would otherwise miss: a dominant file and referenced but uncaptured directories."""
    out = []
    counts = collections.Counter(n["path"] for n in graph.nodes.values() if n["kind"] != "directory" and n.get("path"))
    total = sum(counts.values())
    for path, count in counts.most_common():
        if not total or count / total <= DOMINANT_SHARE:
            break
        out.append({"kind": "dominant_file", "path": path, "nodes": count, "share": round(count / total, 3),
                    "meaning": "Counts and rankings skew toward this file; exclude it if vendored or generated."})
    captured = {p.split("/", 1)[0] for p in graph.files}
    try:
        missing = sorted(e.name for e in os.scandir(root) if e.is_dir(follow_symlinks=False)
                         and e.name not in captured and e.name not in UNREFERENCED)
    except OSError:
        missing = []
    for name in missing:
        mention = re.compile(rf"(?<![\w.-])(?:\./)?{re.escape(name)}/")
        refs = [{"path": row["path"], "line": number}
                for row in graph.body["source"]["files"]
                if row["text"] is not None and not row["path"].lower().endswith(QUIET)
                for number, line in enumerate(row["text"].splitlines(), 1) if mention.search(line)]
        if refs:
            out.append({"kind": "uncaptured_reference", "path": name, "references": len(refs), "examples": refs[:5],
                        "basis": "current disk", "meaning": "Captured files use this directory, but it is excluded "
                        "or ignored; read it directly before judging what those files do."})
    return out


def evidence(graph, item):
    result = dict(item)
    source = item.get("evidence")
    if source:
        row = next((r for r in graph.body["source"]["files"] if r["path"] == source["path"]), None)
        if row and row["text"] is not None:
            lines = row["text"].splitlines()
            first, last = source["line"], source["end_line"]
            result["source"] = {**source, "text": "\n".join(lines[first-1:last]),
                                "ref": reference(graph.snapshot, "source", row["path"])}
    elif item.get("kind") in ("file", "symbol"):
        result["source_ref"] = reference(graph.snapshot, "source", item["path"])
    return result


def changes(before, after):
    old = {f["path"]: f for f in before.body["source"]["files"]}
    new = {f["path"]: f for f in after.body["source"]["files"]}
    rows = []
    for path in sorted(old.keys() | new.keys()):
        a, b = old.get(path), new.get(path)
        exists = lambda r: r is not None and r["state"] != "missing"
        if a and b and (a["digest"], a["state"]) == (b["digest"], b["state"]) and a["digest"]:
            continue
        if not exists(a) and not exists(b):
            continue
        complete = lambda g: g.body["coverage"]["inventory_complete"] and g.body["coverage"]["capture_stable"]
        if not exists(a):
            state = "added" if complete(before) and b["digest"] else "unknown"
        elif not exists(b):
            state = "deleted" if complete(after) and a["digest"] else "unknown"
        elif a["digest"] and b["digest"]:
            state = "modified"
        else:
            state = "unknown"
        rows.append({"kind": "change", "path": path, "state": state,
                     "before_digest": a["digest"] if a else None, "after_digest": b["digest"] if b else None,
                     "before_ref": reference(before.snapshot, "source", path) if a else None,
                     "after_ref": reference(after.snapshot, "source", path) if b else None})
    return rows


def impact(graph, targets, base=None, depth=2, max_nodes=1000, seconds=5.0):
    if base:
        if base.snapshot == graph.snapshot:
            raise ValueError("Impact comparison requires distinct captured snapshots")
        delta = changes(base, graph)
        roots = {r["path"] for r in delta}
    else:
        roots, widened = set(), []
        for target in targets:
            node = graph.nodes[graph.unique(target)]
            if node["kind"] == "symbol":
                # Impact runs at file grain: every symbol in the file seeds the traversal.
                widened.append({"target": target, "resolved": graph.public(node), "projected_to": node["path"]})
            for ident in graph.seeds(target, "file"):
                path = graph.nodes[ident].get("path")
                if path not in graph.files:
                    raise ValueError(f"Impact target has no captured source file: {target}")
                roots.add(path)
        if not roots:
            raise ValueError("Supply a source target, or --base and --head snapshots")
        delta = [{"kind": "change", "state": "planned", "path": p,
                  "ref": reference(graph.snapshot, "source", p)} for p in sorted(roots)]
    gaps = [] if base else [{"area": "target", "reason": "widened_to_file", "grain": "file", **w} for w in widened]
    results, traversals, sides = list(delta), {}, [("candidate", graph)]
    if base:
        sides.insert(0, ("base", base))
    for side, current in sides:
        seeds = {current.files[p]["id"] for p in roots if p in current.files}
        reached = set(seeds)
        consumers = []
        traversals[side] = {}
        for direction, role in (("in", "possible_consumer"), ("out", "dependency_context")):
            rows, limits = current.walk(seeds, direction=direction, depth=depth, max_nodes=max_nodes,
                                        impact=True, seconds=seconds)
            traversals[side][direction] = limits
            results.extend({**r, "role": role, "side": side} for r in rows)
            reached.update(r["id"] for r in rows)
            if direction == "in":
                consumers = rows
            if limits["bounds"]:
                gaps.append({"area": "traversal", "reason": "bounded", "side": side, "direction": direction,
                             "bounds": limits["bounds"], "frontier": limits["frontier"]})
        paths = {current.nodes[n].get("path") for n in reached}
        if not base:
            for target in targets:
                gap = unresolved_mentions(current, target, paths)
                if gap:
                    gaps.append(gap)
        tests = collections.defaultdict(list)
        for edge in current.body["edges"]:
            if edge["predicate"] == "TESTS" and edge["target"] in reached:
                tests[edge["source"]].append(reference(current.snapshot, "edges", edge["id"]))
        for test, refs in sorted(tests.items()):
            results.append({"kind": "verification", "side": side, "test": current.public(current.nodes[test]),
                            "evidence_refs": refs, "basis": "static_import_association", "execution": "not_run"})
        public = [current.public(n) for n in current.nodes.values() if n.get("public_candidate") and n.get("path") in paths]
        results.append({"kind": "risk_profile", "side": side, "possible_consumers": len(consumers),
                        "test_candidates": len(tests), "public_symbol_candidates": public,
                        "unknowns_in_reached_files": sum(g.get("path") in paths for g in current.body["gaps"]),
                        "external_consumers": "unknown", "behavioral_effect": "not_verified",
                        "design_fit": "requires requirements, ownership and lifecycle interpretation"})
        gaps.extend({**g, "side": side} for g in current.body["gaps"])
    if base:
        def keyed(current):
            def node_key(ident):
                n = current.nodes[ident]
                return (n["kind"], n.get("path"), n.get("qualname") or n.get("name"), n.get("symbol_kind"))
            return {(node_key(e["source"]), e["predicate"], node_key(e["target"])): e
                    for e in current.body["edges"] if e["predicate"] in DEPENDENCIES}
        old, new = keyed(base), keyed(graph)
        for label, current, left, right in (("observed_only_in_base", base, old, new), ("observed_only_in_candidate", graph, new, old)):
            for key in sorted(left.keys() - right.keys(), key=str):
                results.append({"kind": "relationship_change", "state": label,
                                "edge": reference(current.snapshot, "edges", left[key]["id"])})
        deleted = collections.defaultdict(list)
        added = collections.defaultdict(list)
        for row in delta:
            if row["state"] == "deleted":
                deleted[row["before_digest"]].append(row["path"])
            if row["state"] == "added":
                added[row["after_digest"]].append(row["path"])
        for sha in sorted(deleted.keys() & added.keys()):
            results.append({"kind": "correspondence", "basis": "equal_file_content", "before": deleted[sha],
                            "after": added[sha], "state": "unique_content_match" if len(deleted[sha]) == len(added[sha]) == 1 else "ambiguous",
                            "limitation": "Matching bytes do not establish enduring identity or intent; no graph remapping."})
    return {"operation": "impact", "snapshots": [g.snapshot for _, g in sides], "results": results, "gaps": gaps,
            "scope": {"mode": "comparison" if base else "planned", "grain": "file", "traversal": traversals,
                      "targets": [] if base else list(targets), "widened_targets": [] if base else widened,
                      "assessment": "potential impact, not verified breakage", "full_inventory_delta": bool(base)},
            "coverage": {side: current.body["coverage"] for side, current in sides}}


TABLES = {
    "nodes": {"id": "TEXT", "kind": "TEXT", "key": "TEXT", "path": "TEXT", "name": "TEXT", "qualname": "TEXT",
              "line": "INTEGER", "end_line": "INTEGER", "symbol_kind": "TEXT", "language": "TEXT", "is_test": "INTEGER", "ref": "TEXT"},
    "edges": {"id": "TEXT", "source": "TEXT", "predicate": "TEXT", "target": "TEXT", "resolution": "TEXT",
              "rule": "TEXT", "evidence": "TEXT", "ref": "TEXT"},
    "gaps": {"area": "TEXT", "reason": "TEXT", "path": "TEXT", "line": "INTEGER", "predicate": "TEXT", "text": "TEXT",
             "rule": "TEXT", "bindings": "INTEGER", "calls": "INTEGER", "language": "TEXT", "error": "TEXT",
             "candidates": "TEXT"},
    "files": {"path": "TEXT", "digest": "TEXT", "state": "TEXT", "size": "INTEGER", "ref": "TEXT"},
}


def query(graph, sql, params=None, limit=1000, seconds=5.0):
    db = sqlite3.connect(":memory:")
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4*1024*1024)
    db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 128*1024)
    sources = {"nodes": [graph.public(n) for n in graph.body["nodes"]],
               "edges": [{**e, "ref": reference(graph.snapshot, "edges", e["id"])} for e in graph.body["edges"]],
               # Python call gaps are one row per call; TS/JS and Rust rows carry their own count.
               "gaps": [{**g, "calls": g.get("calls", 1)} if g["area"] == "calls" else g for g in graph.body["gaps"]],
               "files": [{**r, "ref": reference(graph.snapshot, "source", r["path"])} for r in graph.body["source"]["files"]]}
    def scalar(value):
        return json.dumps(value, ensure_ascii=True) if isinstance(value, (dict, list)) else value
    try:
        for table, columns in TABLES.items():
            db.execute(f'CREATE TABLE "{table}" (' + ",".join(f'"{n}" {t}' for n, t in columns.items()) + ")")
            db.executemany(f'INSERT INTO "{table}" VALUES (' + ",".join("?" for _ in columns) + ")",
                           [[scalar(r.get(n)) for n in columns] for r in sources[table]])
        db.commit()
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
        def authorize(action, arg1, arg2, database, trigger):
            if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in ("load_extension", "writefile", "readfile"):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY
        db.set_authorizer(authorize)
        deadline = time.monotonic() + seconds
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        cursor = db.execute(sql, params or {})
        if cursor.description is None:
            raise ValueError("Use a read-only SELECT or WITH query")
        names = [x[0] for x in cursor.description]
        if len(set(names)) != len(names):
            raise ValueError("Alias duplicate output column names")
        rows = cursor.fetchmany(limit + 1)
        def output_value(value):
            if isinstance(value, bytes):
                return {"encoding": "hex", "value": value.hex()}
            if isinstance(value, float) and not math.isfinite(value):
                return str(value)
            return value
        result = graph.document("query", [dict(zip(names, map(output_value, row))) for row in rows[:limit]], engine="sqlite",
                                query=sql, query_complete=len(rows) <= limit, max_query_rows=limit)
        if len(rows) > limit:
            result["gaps"] = [*result["gaps"], {"area": "query", "reason": "row_limit", "limit": limit}]
        return result
    except sqlite3.Error as error:
        raise ValueError(f"Read-only query failed: {error}") from None
    finally:
        db.close()
