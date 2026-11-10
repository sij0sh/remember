#!/usr/bin/env python3
"""Graph commands: map, find and show over a captured source snapshot."""
import collections

from .graph_queries import Graph, evidence, warnings, path_matches
from .graph_store import require_current
from .whole_repo import MAX_BYTES, TEXT_BYTES, render, summary


def get_graph(store, args, pin=None):
    pin = pin or args.snapshot
    if pin:
        if not pin.startswith("snapshot:"):
            raise ValueError("Expected a snapshot ID")
        body = store.load(pin)
        return Graph(pin, body)
    pin, body = store.snapshot(max_files=args.max_files, max_file_bytes=args.max_file_bytes,
                               max_total_bytes=args.max_total_bytes, exclude=args.exclude)
    return Graph(pin, body)


def show(store, args):
    key, *tail = args.ref.split("/", 2)
    body = store.load(key)
    graph = Graph(key, body)
    if not tail:
        return graph.document("show", [{"snapshot": key, "root": body["source"]["root"],
                                        "inventory": body["source"]["inventory"]}])
    if len(tail) != 2:
        raise ValueError("Use a returned source, node or edge reference")
    section, target = tail
    if section == "source":
        row = next((r for r in body["source"]["files"] if r["path"] == target), None)
        if row is None:
            raise ValueError("Source path is not present in this snapshot")
        require_current(key, row)
        if row["text"] is None:
            return graph.document("show", [{k: v for k, v in row.items() if k != "text"}])
        lines = row["text"].splitlines()
        start = args.start_line - 1
        results = [{"path": target, "line": i+1, "text": line, "digest": row["digest"]}
                   for i, line in enumerate(lines[start:start+args.lines], start)]
        return graph.document("show", results, source_ref=args.ref, total_lines=len(lines),
                              next_line=start+args.lines+1 if start+args.lines < len(lines) else None)
    if section not in ("nodes", "edges"):
        raise ValueError("Unknown reference section")
    item = next((r for r in body[section] if r["id"] == target), None)
    if item is None:
        raise ValueError("Reference not present in this snapshot")
    return graph.document("show", [evidence(graph, item)])



def locate(args, store):
    """map and find over one captured snapshot."""
    graph = get_graph(store, args)
    if args.action == "map":
        if not args.path and not args.kind and not args.format and not args.fields:
            found = warnings(graph, store.root)
            recent = None
            if args.primer and args.snapshot:
                recent = ["primer omitted: --snapshot pins source; the primer reads live history"]
            elif args.primer:
                try:
                    from . import orientation
                    recent = orientation.render(orientation.build(graph, args, store), args.primer_bytes)
                except Exception as error:
                    # Orientation must not fail because a recording or Git lookup is unavailable.
                    recent = [f"primer omitted: {type(error).__name__}: {str(error)[:120]}"]
            text = render(graph, found, args.whole_repo_lines, recent, MAX_BYTES if args.whole else TEXT_BYTES)
            if text is None and args.whole_repo_lines > 0:
                text = summary(graph, found, args.whole_repo_lines, recent)
            if text is not None:
                return {"text": text}
        kinds = getattr(args, "kind", None) or ["file", "directory"]
        paths = graph.within(args.path) if args.path else None
        rows = [graph.public(n) for n in graph.nodes.values()
                if n["kind"] in kinds and (paths is None or n.get("path") in paths)]
        rows.sort(key=lambda n: (n.get("path") or "", n.get("line") or 0, n["kind"]))
        doc = graph.document(args.action, rows, kinds=kinds, path=args.path)
        if args.primer and args.primer_explicit and not args.snapshot:
            from . import orientation
            doc["primer"] = orientation.build(graph, args, store)
        elif args.primer and args.snapshot:
            doc["primer"] = "omitted: --snapshot pins source; the primer reads live history"
        elif not args.primer_explicit and not args.offset:
            doc["primer"] = "omitted: --path/--kind/--format/--fields given; add --primer"
    elif args.action == "find":
        if args.regex:
            if args.text is not None:
                raise ValueError("Supply TEXT or --regex PATTERN, not both")
            args.text = args.regex
        if not args.text or not args.text.strip():
            raise ValueError("Search text must not be empty")
        rows = graph.find(args.text, word=args.word, case_sensitive=args.case_sensitive, regex=bool(args.regex))
        rows = [r for r in rows if (not args.path or path_matches(r.get("path"), args.path))
                and not path_matches(r.get("path"), args.not_path)]
        if args.kind:
            rows = [r for r in rows if r["kind"] in args.kind]
        if args.count_by:
            grouped = collections.defaultdict(list)
            for row in rows:
                grouped[row.get("path")].append(row)
            rows = [{"path": path, "count": len(group), "kinds": sorted({r["kind"] for r in group}),
                     "first_line": min((r["line"] for r in group if r.get("line")), default=None)}
                    for path, group in grouped.items()]
            rows.sort(key=lambda r: (-r["count"], r["path"] or ""))
        doc = graph.document("find", rows, path=args.path, not_path=args.not_path, count_by=args.count_by, text=args.text, kinds=args.kind,
                             match={"word": args.word, "case_sensitive": args.case_sensitive, "regex": args.regex})
    else:
        raise ValueError("Unknown operation")
    doc["scope"]["freshness"] = store.freshness(graph.body) if args.snapshot else {"state": "captured_now"}
    doc["incomplete"] = incomplete(doc, graph)
    return doc


# Gaps that could change the answer make a result incomplete; paging and byte budgets only truncate it.
# Import, call and target gaps describe relationships the graph lacks and never limit by themselves.
WHOLE = {"inventory", "capture", "traversal", "mentions"}
UNREAD = {"file_limit", "byte_limit", "unread", "changed_during_capture", "missing"}
SCANNING = {"find", "query"}


def incomplete(document, graph=None):
    """Gaps that could change this answer, as opposed to gaps elsewhere in the snapshot."""
    paths = {r.get("path") for r in document["results"]}
    paths |= {p for r in document["results"] for p in r.get("files") or ()}
    scope = document["scope"]
    tests = scope.get("include_tests", True)
    files = graph.files if graph else {}
    found = []
    for gap in document["gaps"]:
        area, path = gap.get("area"), gap.get("path")
        if area in WHOLE or (area == "query" and gap.get("reason") != "row_limit"):
            found.append(gap)
        elif area in ("source", "parser"):
            # A file the scan could not read or parse may hold a match or rank; elsewhere only its own rows suffer.
            node = files.get(path) or {}
            skipped = node.get("is_test") and not tests
            scanned = document["operation"] in SCANNING and (area == "parser" or gap.get("reason") in UNREAD)
            if not path or path in paths or (scanned and not skipped):
                found.append(gap)
    return found
