#!/usr/bin/env python3
"""Map repository evidence and potential change impact. History is optional."""
import argparse
import json
import sys
from pathlib import Path

from helper.graph_build import DEPENDENCIES, PREDICATES
from helper.graph_queries import Graph, TABLES, evidence, impact, query, reference
from helper.graph_store import Store, canonical

COMMANDS = {
    "map": "List source files, directories or symbols and graph coverage (default).",
    "snapshot": "Capture source and retain an immutable graph; return its ID.",
    "find": "Locate names, paths and literal source occurrences; matches are candidates.",
    "neighbors": "Traverse typed incoming/outgoing relationships with witness paths.",
    "paths": "Find a shortest supported path between targets, within traversal bounds.",
    "impact": "Assess planned targets or the full delta between two retained snapshots.",
    "show": "Expand retained source, nodes, edges or report pages without refreshing them.",
    "query": "Run one read-only SQLite SELECT/CTE over nodes, edges, files and gaps.",
    "status": "Inspect graph coverage, producer capabilities and current source freshness.",
    "describe": "Describe commands, predicates and SQL tables without scanning source.",
}


def positive(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def nonnegative(value):
    value = int(value)
    if value < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return value


def parser():
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("--repo", help="Source root, including an ordinary directory; default current directory")
    common.add_argument("--cache", help="Cache parent outside source tree; default $XDG_CACHE_HOME/remember")
    common.add_argument("--snapshot", help="Use a retained snapshot instead of capturing current source")
    common.add_argument("--exclude", action="append", help="Exclude relative-path/component glob; repeatable")
    common.add_argument("--max-files", type=positive, help="Capture file count limit (20000)")
    common.add_argument("--max-file-bytes", type=positive, help="Per-file read limit (1 MiB)")
    common.add_argument("--max-total-bytes", type=positive, help="Total captured bytes (32 MiB)")
    common.add_argument("--limit", type=positive, help="Delivered rows per page (30); does not limit traversal")
    common.add_argument("--offset", type=nonnegative, help="Delivered row offset (0)")
    common.add_argument("--max-bytes", type=positive, help="JSON response budget, at least 2048 (12000)")
    p = argparse.ArgumentParser(description=__doc__, parents=[common])
    sub = p.add_subparsers(dest="action")
    for name, description in COMMANDS.items():
        q = sub.add_parser(name, help=description, description=description, parents=[common])
        if name in ("map", "find"):
            q.add_argument("--kind", action="append", choices=("file", "directory", "symbol", "external", "occurrence"))
        if name == "find":
            q.add_argument("text")
        if name in ("neighbors", "paths", "impact"):
            q.add_argument("--depth", type=positive, default=2, help="Traversal depth; actual omitted frontier is reported")
            q.add_argument("--max-nodes", type=positive, default=1000)
            q.add_argument("--seconds", type=positive, default=5, help="Deadline per traversal")
        if name in ("neighbors", "paths"):
            q.add_argument("--predicate", action="append", choices=tuple(PREDICATES), help="Repeat to choose edge types; default IMPORTS,CALLS_STATIC")
            q.add_argument("--grain", choices=("file", "symbol"), default="file", help="File projection or original symbol graph")
        if name == "neighbors":
            q.add_argument("target")
            q.add_argument("--direction", choices=("in", "out", "both"), default="in")
        if name == "paths":
            q.add_argument("source")
            q.add_argument("target")
        if name == "impact":
            q.add_argument("targets", nargs="*")
            q.add_argument("--target", action="append", default=[], help="Additional planned target")
            q.add_argument("--base", help="Baseline snapshot for full comparison")
            q.add_argument("--head", help="Candidate snapshot for full comparison")
        if name == "show":
            q.add_argument("ref", help="Returned snapshot/report/source/node/edge reference")
            q.add_argument("--start-line", type=positive, default=1)
            q.add_argument("--lines", type=positive, default=80)
        if name == "query":
            q.add_argument("--sql", required=True)
            q.add_argument("--param", action="append", default=[], help="Bind NAME=VALUE to :NAME")
            q.add_argument("--max-query-rows", type=positive, default=1000)
            q.add_argument("--seconds", type=positive, default=5)
    return p


def defaults(args):
    for key, value in {"repo": ".", "cache": None, "snapshot": None, "exclude": [], "max_files": 20000,
                       "max_file_bytes": 1024*1024, "max_total_bytes": 32*1024*1024,
                       "limit": 30, "offset": 0, "max_bytes": 12000}.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    args.action = args.action or "map"
    if args.max_bytes < 2048:
        raise ValueError("--max-bytes must be at least 2048")
    return args


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
    if key.startswith("report:"):
        if not tail:
            return body
        section = tail[0]
        if section not in ("gaps", "results", "coverage", "scope") or len(tail) != 1:
            raise ValueError("Report sections: results, gaps, coverage, scope")
        rows = body.get(section, [])
        return {"operation": "show", "snapshots": body.get("snapshots", []),
                "results": rows if isinstance(rows, list) else [rows], "gaps": [],
                "coverage": {}, "scope": {"report": key, "section": section}}
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
        raise ValueError("Reference not present in retained snapshot")
    return graph.document("show", [evidence(graph, item)])


def describe(p):
    sub = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
    return {"operation": "describe", "schema": 1, "commands": [
        {"name": name, "description": COMMANDS[name], "arguments": [
            {"name": a.dest, "flags": a.option_strings, "required": a.required,
             "choices": list(a.choices) if a.choices else None, "help": a.help}
            for a in command._actions if a.dest != "help"]}
        for name, command in sub.choices.items()], "predicates": PREDICATES, "tables": TABLES,
        "history": "sessions.py and rows.py remain separate optional tools; graph commands do not read recordings."}


def run(args, store):
    if args.action == "show":
        return show(store, args)
    if args.action == "impact":
        if bool(args.base) != bool(args.head):
            raise ValueError("Comparison requires both --base and --head")
        if args.base and (args.targets or args.target or args.snapshot):
            raise ValueError("Comparison evaluates the full delta; do not supply targets or --snapshot")
        current = get_graph(store, args, args.head)
        base = get_graph(store, args, args.base) if args.base else None
        doc = impact(current, args.targets + args.target, base, args.depth, args.max_nodes, args.seconds)
        doc["scope"]["freshness"] = store.freshness(current.body) if args.head or args.snapshot else {"state": "captured_now"}
        return doc
    graph = get_graph(store, args)
    if args.action in ("map", "snapshot", "status"):
        if args.action == "map":
            kinds = getattr(args, "kind", None) or ["file", "directory"]
            rows = [graph.public(n) for n in graph.nodes.values() if n["kind"] in kinds]
            rows.sort(key=lambda n: (n.get("path") or "", n.get("line") or 0, n["kind"]))
            doc = graph.document(args.action, rows, kinds=kinds)
        else:
            rows = [{"snapshot": graph.snapshot, "root": store.root, "git": graph.body["source"]["git"],
                     "inventory": graph.body["source"]["inventory"], "producer": graph.body["producer"]}]
            doc = graph.document(args.action, rows)
    elif args.action == "find":
        if not args.text.strip():
            raise ValueError("Search text must not be empty")
        rows = graph.find(args.text)
        if args.kind:
            rows = [r for r in rows if r["kind"] in args.kind]
        doc = graph.document("find", rows, text=args.text, kinds=args.kind)
    elif args.action in ("neighbors", "paths"):
        source = args.source if args.action == "paths" else args.target
        direction = "out" if args.action == "paths" else args.direction
        seeds = graph.seeds(source, args.grain)
        rows, limits = graph.walk(seeds, direction, args.predicate or DEPENDENCIES, args.grain,
                                  args.depth, args.max_nodes, seconds=args.seconds)
        if args.action == "paths":
            dest = graph.seeds(args.target, args.grain)
            rows = [r for r in rows if r["id"] in dest]
            rows += [{**graph.public(graph.nodes[n]), "distance": 0, "witness": []} for n in sorted(dest & seeds)]
        targets = {"source": args.source, "target": args.target} if args.action == "paths" else {"target": args.target}
        doc = graph.document(args.action, rows, **targets, traversal=limits, grain=args.grain,
                              predicates=args.predicate or list(DEPENDENCIES), direction=direction,
                              path_result=("supported_path" if rows else "no_supported_path_within_scope") if args.action == "paths" else None)
        if limits["bounds"]:
            doc["gaps"] = [*doc["gaps"], {"area": "traversal", "reason": "bounded", **limits}]
    elif args.action == "query":
        params = {}
        for item in args.param:
            name, sep, value = item.partition("=")
            if not sep or not name.isidentifier():
                raise ValueError("--param requires NAME=VALUE")
            params[name] = value
        doc = query(graph, args.sql, params, args.max_query_rows, args.seconds)
    else:
        raise ValueError("Unknown operation")
    doc["scope"]["freshness"] = store.freshness(graph.body) if args.snapshot or args.action == "status" else {"state": "captured_now"}
    return doc


# Gap areas that limit a result; import, call and target gaps describe relationships the graph lacks.
LIMITING = {"inventory", "capture", "source", "parser", "traversal", "query"}


def deliver(store, document, args):
    report = store.save("report", document)
    all_rows = document["results"]
    rows = all_rows[args.offset:args.offset+args.limit]
    limited = any(g.get("area") in LIMITING for g in document["gaps"])
    output = {"schema_version": 1, "status": "partial" if limited else "ok", "report": report,
              "snapshots": document.get("snapshots", []), "operation": document["operation"],
              "scope": document["scope"], "coverage": document["coverage"], "results": rows,
              "gaps": {"count": len(document["gaps"]), "ref": report+"/gaps"},
              "delivery": {"offset": args.offset, "total": len(all_rows)}}
    def finish():
        output["delivery"].update(returned=len(output["results"]), complete=args.offset == 0 and len(output["results"]) == len(all_rows))
        missing = len(all_rows) - len(output["results"])
        output["omissions"] = {"selection": missing}
        if missing:
            output["status"] = "partial"
        if missing and not output["results"] and args.offset < len(all_rows):
            output["omissions"]["envelope_omitted"] = True
        following = args.offset + len(output["results"])
        output["next"] = ({"action": "show", "ref": report, "repo": store.root,
                           "cache": str(store.path.parent), "offset": following}
                          if following < len(all_rows) else None)
        if output["next"] and not output["results"]:
            output["next"]["hint"] = "Increase --max-bytes; this row does not fit."
    finish()
    # Keep complete evidence on disk; trim delivery without recomputing analysis.
    if len(canonical(output)) + 1 > args.max_bytes:
        output["scope"] = {"ref": report+"/scope", "freshness": document["scope"].get("freshness")}
        output["coverage"] = {"ref": report+"/coverage"}
    while output["results"] and len(canonical(output)) + 1 > args.max_bytes:
        output["results"].pop()
        finish()
    if len(canonical(output)) + 1 > args.max_bytes:
        output = {"schema_version": 1, "status": "partial", "report": report, "results": [],
                  "omissions": {"envelope_omitted": True},
                  "delivery": {"complete": False, "reason": "metadata_budget", "total": len(all_rows)},
                  "next": {"action": "show", "ref": report, "hint": "Increase --max-bytes"}}
    print(canonical(output).decode())


def main():
    p = parser()
    try:
        args = defaults(p.parse_args())
        if args.action == "describe":
            schema = describe(p)
            rows = [{"kind": "command", **command} for command in schema["commands"]]
            rows += [{"kind": "predicate", "name": k, "description": v} for k, v in PREDICATES.items()]
            rows += [{"kind": "table", "name": k, "columns": v} for k, v in TABLES.items()]
            deliver(Store(args.repo, args.cache), {"operation": "describe", "snapshots": [], "results": rows,
                    "gaps": [], "coverage": {}, "scope": {"schema": 1, "history": schema["history"]}}, args)
            return 0
        store = Store(args.repo, args.cache)
        document = run(args, store)
        deliver(store, document, args)
        return 0
    except (ValueError, OSError, RecursionError) as error:
        print(json.dumps({"status": "error", "results": [], "error": str(error)[:4000]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
