#!/usr/bin/env python3
"""Map repository evidence and retrieve target-scoped Git and session history."""
import argparse
import collections
import json
import sys
from pathlib import Path

from helper.graph_build import DEPENDENCIES, PREDICATES
from helper.graph_queries import Graph, TABLES, evidence, impact, query, reference, warnings, path_matches, unresolved_mentions
from helper.graph_store import Store, canonical

from helper.graph_reports import REPORTS, analyze, generated_files
from helper.graph_output import compact_row, project_rows, render_table
from helper.whole_repo import MAX_BYTES, MAX_LINES, TEXT_BYTES, render, summary

COMMANDS = {
    "map": "List source files, directories or symbols and graph coverage (default).",
    "analyze": "Rank structural candidates: fanin, orphans, cycles, untested, largest.",
    "snapshot": "Capture source and retain an immutable graph; return its ID.",
    "find": "Locate names, paths and literal source occurrences; matches are candidates.",
    "neighbors": "Traverse typed incoming/outgoing relationships with witness paths.",
    "paths": "Find a shortest supported path between targets, within traversal bounds.",
    "impact": "Assess planned targets or the full delta between two retained snapshots.",
    "history": "Retrieve bounded Git and recording leads for a file or symbol; output is transient.",
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


class Parser(argparse.ArgumentParser):
    """Unknown options are reported by the chosen subcommand, with that subcommand's usage."""

    def parse_args(self, args=None, namespace=None):
        args, extra = self.parse_known_args(args, namespace)
        if extra:
            action = getattr(args, "action", None)
            choices = next((a.choices for a in self._actions if isinstance(a, argparse._SubParsersAction)), {})
            hint = ("; impact is file-level, --grain applies to neighbors, paths and analyze"
                    if action == "impact" and any(a.startswith("--grain") for a in extra) else "")
            (choices.get(action) or self).error("unrecognized arguments: " + " ".join(extra) + hint)
        return args


def parser():
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("--repo", help="Source root, including an ordinary directory; default current directory")
    common.add_argument("--cache", help="Cache parent outside source tree; default $XDG_CACHE_HOME/remember")
    common.add_argument("--snapshot", help="Use a retained snapshot instead of capturing current source")
    common.add_argument("--exclude", action="append", help="Exclude relative-path/component glob; repeatable")
    common.add_argument("--max-files", type=positive, help="Capture file count limit (20000)")
    common.add_argument("--max-file-bytes", type=positive, help="Per-file read limit (1 MiB)")
    common.add_argument("--max-total-bytes", type=positive, help="Total captured bytes (32 MiB)")
    common.add_argument("--limit", type=positive, help="Rows per page (query: 200, analyze: 60, others: 30); also raises query retention")
    common.add_argument("--offset", type=nonnegative, help="Delivered row offset (0)")
    common.add_argument("--max-bytes", type=positive, help="JSON response budget, at least 2048 (analyze: 24000, others: 12000)")
    common.add_argument("--format", choices=("json", "table"), help="Result format; bare map defaults to text")
    common.add_argument("--fields", help="Comma-separated result columns; full reports remain retained")
    common.add_argument("--verbose", action="store_true",
                        help="Full rows: ids, keys, digests, witness endpoints; history: events and every counter")
    p = Parser(description=__doc__, parents=[common])
    sub = p.add_subparsers(dest="action")
    for name, description in COMMANDS.items():
        q = sub.add_parser(name, help=description, description=description, parents=[common])
        if name in ("map", "find"):
            q.add_argument("--kind", action="append", choices=("file", "directory", "symbol", "external", "occurrence"))
        if name == "map":
            q.add_argument("path", nargs="?", help="Limit rows to this file, directory or symbol's file")
            q.add_argument("--whole-repo-lines", type=nonnegative, default=MAX_LINES,
                           help="Bare map prints every source file as text at or under this many lines; 0 disables")
            q.add_argument("--whole", action="store_true",
                           help=f"Bare map: print whole source up to {MAX_BYTES} bytes (default cap {TEXT_BYTES})")
            q.add_argument("--primer", dest="primer", action="store_true", default=None,
                           help="Include the recent-work primer in JSON map output; bare map text includes it by default")
            q.add_argument("--no-primer", dest="primer", action="store_false",
                           help="Disable the recent-work primer on bare map text")
            q.add_argument("--primer-depth", type=nonnegative, default=3,
                           help="Recent sessions and non-merge commits to show (3); 0 disables")
            q.add_argument("--primer-bytes", type=positive, default=2048,
                           help="Text primer byte budget (2048)")
            q.add_argument("--primer-root", action="append", default=[],
                           help="Recording project root for the primer; repeatable")
            q.add_argument("--primer-merges", action="store_true", default=False,
                           help="Include merge commits in the primer")
            q.add_argument("--exclude-session", action="append", default=[],
                           help="Exclude session ID or recording path, including its lineage")
        if name == "find":
            q.add_argument("text", nargs="?")
            q.add_argument("--word", action="store_true", help="Match whole words only")
            q.add_argument("--case-sensitive", action="store_true", help="Match case exactly (default ignores case)")
            q.add_argument("--regex", nargs="?", const=True, default=False, help="Python regex PATTERN; legacy TEXT --regex also works")
            q.add_argument("--path", action="append", help="Include result path glob or directory; repeatable")
            q.add_argument("--not-path", action="append", default=[], help="Exclude result path glob or directory; repeatable")
            q.add_argument("--count-by", choices=("path",), help="Count matching result rows per path")
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
        if name == "history":
            q.add_argument("target")
            q.add_argument("--recordings", action="append", default=[], help="Recording file/directory; repeatable")
            q.add_argument("--project-root", action="append", default=[], help="Additional historical recording project root")
            q.add_argument("--history-repo", action="append", default=[], help="Explicit old Git repository and path prefix: REPO=PREFIX")
            q.add_argument("--root-map", action="append", default=[], help="Rewrite recorded path prefix OLD=NEW; discovery uses --project-root")
            q.add_argument("--exclude-session", action="append", default=[], help="Exclude session ID or recording path, including its lineage")
            q.add_argument("--no-sessions", action="store_true")
            q.add_argument("--max-commits", type=positive, default=30)
            q.add_argument("--max-recordings", type=positive, default=25, help="Per-harness recording limit")
            q.add_argument("--max-candidates", type=positive, default=1000, help="Per-harness discovery header limit")
            q.add_argument("--max-scan-bytes", type=positive, default=64*1024*1024, help="Per-harness scan byte limit")
            q.add_argument("--max-events", type=positive, default=25000, help="Per-harness event limit")
        if name == "analyze":
            q.add_argument("report", choices=tuple(REPORTS))
            q.add_argument("--grain", "--kind", dest="grain", choices=("file", "symbol"),
                           help="Rank files or symbols; --kind is an alias")
            q.add_argument("--include-tests", action="store_true")
            q.add_argument("--include-generated", action="store_true",
                           help="Rank generated/vendored files too (dist/, vendor/, @generated, large .d.ts)")
            q.add_argument("--include-type-only", action="store_true",
                           help="cycles: count TS `import type` edges, which are erased at runtime")
            q.add_argument("--exclude-entrypoints", action="store_true")
            q.add_argument("--exclude-public", action="store_true")
        if name == "show":
            q.add_argument("ref", help="Returned snapshot/report/source/node/edge reference")
            q.add_argument("--start-line", type=positive, default=1)
            q.add_argument("--lines", type=positive, default=80)
        if name == "query":
            q.add_argument("--sql", required=True)
            q.add_argument("--param", action="append", default=[], help="Bind NAME=VALUE to :NAME")
            q.add_argument("--max-query-rows", type=positive)
            q.add_argument("--seconds", type=positive, default=5)
    return p


def defaults(args):
    args.action = args.action or "map"
    explicit_limit = getattr(args, "limit", None)
    if args.action == "query":
        args.limit = explicit_limit or args.max_query_rows or 200
        args.max_query_rows = max(args.max_query_rows or 1000, args.limit + getattr(args, "offset", 0))
    # Ranked reports are read top-down; a larger first page saves a paging round trip.
    first_page = (60, 24000) if args.action == "analyze" else (30, 12000)
    for key, value in {"repo": ".", "cache": None, "snapshot": None, "exclude": [], "max_files": 20000,
                       "max_file_bytes": 1024*1024, "max_total_bytes": 32*1024*1024,
                       "limit": first_page[0], "offset": 0, "max_bytes": first_page[1], "verbose": False,
                       "format": None, "fields": None, "path": None, "kind": None, "whole_repo_lines": MAX_LINES,
                       "whole": False}.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    if args.max_bytes < 2048:
        raise ValueError("--max-bytes must be at least 2048")
    if args.fields is not None:
        args.fields = [f.strip() for f in args.fields.split(",") if f.strip()]
        if not args.fields:
            raise ValueError("--fields requires at least one column")
    if args.action == "map":
        # A bare invocation has no map subparser, so its options are absent.
        for key, value in {"primer": None, "primer_depth": 3, "primer_bytes": 2048,
                           "primer_root": [], "primer_merges": False, "exclude_session": []}.items():
            if not hasattr(args, key):
                setattr(args, key, value)
        text_mode = not (args.path or args.kind or args.format or args.fields)
        explicit = getattr(args, "primer", None)
        args.primer_explicit = explicit is not None
        args.primer = text_mode if explicit is None else explicit
        if args.primer_depth > 10:
            raise ValueError("--primer-depth must be at most 10")
        if args.primer_bytes < 512:
            raise ValueError("--primer-bytes must be at least 512")
    else:
        args.primer = False
        args.primer_explicit = False
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
        "history": "history TARGET retrieves live Git/session leads explicitly; only bare map reads a bounded recent-work primer."}


def run(args, store):
    from helper.target_history import attach
    if args.action == "history":
        from helper.target_history import run as history
        return history(args, store, get_graph)
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
        if not base:
            attach(doc, current, args.targets + args.target, store)
        return doc
    graph = get_graph(store, args)
    if args.action in ("map", "snapshot", "status"):
        if args.action == "map" and not args.path and not args.kind and not args.format and not args.fields:
            found = warnings(graph, store.root)
            recent = None
            if args.primer and args.snapshot:
                recent = ["primer omitted: --snapshot pins source; the primer reads live history"]
            elif args.primer and args.primer_depth:
                try:
                    from helper.primer import build as build_primer, render as render_primer
                    recent = render_primer(build_primer(graph, args), args.primer_bytes)
                except Exception as error:
                    # Orientation must not fail because a recording or Git lookup is unavailable.
                    recent = [f"primer omitted: {type(error).__name__}: {str(error)[:120]}"]
            text = render(graph, found, args.whole_repo_lines, recent, MAX_BYTES if args.whole else TEXT_BYTES)
            if text is None and args.whole_repo_lines > 0:
                text = summary(graph, found, args.whole_repo_lines, recent)
            if text is not None:
                return {"text": text}
        if args.action == "map":
            kinds = getattr(args, "kind", None) or ["file", "directory"]
            paths = graph.within(args.path) if args.path else None
            rows = [graph.public(n) for n in graph.nodes.values()
                    if n["kind"] in kinds and (paths is None or n.get("path") in paths)]
            rows.sort(key=lambda n: (n.get("path") or "", n.get("line") or 0, n["kind"]))
            doc = graph.document(args.action, rows, kinds=kinds, path=args.path)
            if args.primer and args.primer_explicit and args.primer_depth and not args.snapshot:
                from helper.primer import build as build_primer
                doc["primer"] = build_primer(graph, args)
            elif args.primer and args.snapshot:
                doc["primer"] = "omitted: --snapshot pins source; the primer reads live history"
            elif not args.primer_explicit and not args.offset:
                doc["primer"] = "omitted: --path/--kind/--format/--fields given; add --primer"
        else:
            rows = [{"snapshot": graph.snapshot, "root": store.root, "git": graph.body["source"]["git"],
                     "inventory": graph.body["source"]["inventory"], "producer": graph.body["producer"]}]
            if args.action == "status":
                rows[0]["warnings"] = warnings(graph, store.root)
            doc = graph.document(args.action, rows)
    elif args.action == "analyze":
        doc = analyze(graph, args.report, args.grain, args.include_tests,
                      args.exclude_entrypoints, args.exclude_public, args.include_generated, args.include_type_only)
    elif args.action == "find":
        if isinstance(args.regex, str):
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
        if args.action == "paths" or direction in ("in", "both"):
            excluded = {r.get("path") for r in rows} if args.grain == "file" else set()
            gap = unresolved_mentions(graph, args.target, excluded)
            if gap:
                doc["gaps"] = [*doc["gaps"], gap]
                doc["scope"]["unresolved_mentions"] = gap
                if not rows:
                    doc["scope"]["assessment"] = "unknown"
            if args.action == "paths" and not rows and any(
                    g.get("area") in ("calls", "imports", "mentions") for g in doc["gaps"]):
                doc["scope"]["path_result"] = "no_path_gaps_present"
                doc["scope"]["assessment"] = "unknown"
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
    if args.action in ("map", "neighbors") and (args.path if args.action == "map" else args.target):
        attach(doc, graph, [args.path if args.action == "map" else args.target], store)
    elif args.action == "find":
        candidates = [r for r in doc["results"] if r.get("kind") in ("file", "symbol")]
        if len(candidates) == 1:
            attach(doc, graph, [candidates[0]["ref"]], store)
    doc["scope"]["freshness"] = store.freshness(graph.body) if args.snapshot or args.action == "status" else {"state": "captured_now"}
    doc["incomplete"] = incomplete(doc, graph)
    return doc


# Gaps that could change the answer make a result incomplete; paging and byte budgets only truncate it.
# Import, call and target gaps describe relationships the graph lacks and never limit by themselves.
WHOLE = {"inventory", "capture", "traversal", "mentions"}
UNREAD = {"file_limit", "byte_limit", "unread", "changed_during_capture", "missing"}
SCANNING = {"find", "query", "analyze"}


def incomplete(document, graph=None):
    """Gaps that could change this answer, as opposed to gaps elsewhere in the snapshot."""
    paths = {r.get("path") for r in document["results"]}
    paths |= {p for r in document["results"] for p in r.get("files") or ()}
    scope = document["scope"]
    tests = scope.get("include_tests", document["operation"] != "analyze")
    files = graph.files if graph else {}
    generated = ({r["path"] for r in generated_files(graph)}
                 if graph and document["operation"] == "analyze" and not scope.get("include_generated") else set())
    found = []
    for gap in document["gaps"]:
        area, path = gap.get("area"), gap.get("path")
        if area in WHOLE or (area == "query" and gap.get("reason") != "row_limit"):
            found.append(gap)
        elif area in ("source", "parser"):
            # A file the scan could not read or parse may hold a match or rank; elsewhere only its own rows suffer.
            node = files.get(path) or {}
            skipped = path in generated or (node.get("is_test") and not tests)
            scanned = document["operation"] in SCANNING and (area == "parser" or gap.get("reason") in UNREAD)
            if not path or path in paths or (scanned and not skipped):
                found.append(gap)
    return found


def deliver(store, document, args):
    # Primer evidence is transient and must not be retained with the report.
    primer = document.pop("primer", None)
    limited = document.pop("incomplete", None)
    limited = incomplete(document) if limited is None else limited
    report = store.save("report", document)
    all_rows = document["results"]
    rows = all_rows[args.offset:args.offset+args.limit]
    if not args.verbose and not args.fields and document["operation"] != "query":
        rows = [compact_row(r) for r in rows]
    rows = project_rows(rows, args.fields)
    output = {"schema_version": 1, "status": "partial" if limited else "ok", "report": report,
              "snapshots": document.get("snapshots", []), "operation": document["operation"],
              "scope": document["scope"], "coverage": document["coverage"], "results": rows,
              "gaps": {"count": len(document["gaps"]), "incomplete": len(limited), "ref": report+"/gaps"},
              "delivery": {"offset": args.offset, "total": len(all_rows)}}
    if document["scope"].get("assessment") == "unknown":
        output["status"] = "unknown"
    if "history" in document:
        output["history"] = document["history"]
    if primer is not None:
        if args.format == "table" and isinstance(primer, dict):
            from helper.primer import render as render_primer
            primer = render_primer(primer, args.primer_bytes)
        output["primer"] = primer
    def finish():
        # A page is complete when it reaches the last row; earlier pages were delivered by earlier calls.
        following = args.offset + len(output["results"])
        output["delivery"].update(returned=len(output["results"]), complete=following >= len(all_rows))
        missing = max(0, len(all_rows) - following)
        output["omissions"] = {"selection": missing}
        query_limited = any(g.get("area") == "query" and g.get("reason") == "row_limit" for g in document["gaps"])
        output.pop("results_truncated", None)
        if missing or query_limited:
            output["results_truncated"] = (f"{len(output['results'])} of {len(all_rows)} retained rows shown; "
                f"--offset {following} or raise --limit N / --max-bytes N"
                + ("; query retention capped: rerun query with a larger --limit" if query_limited else ""))
        if missing and not output["results"] and args.offset < len(all_rows):
            output["omissions"]["envelope_omitted"] = True
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
    # A requested primer that would crowd out every row says so instead of vanishing.
    if isinstance(output.get("primer"), (dict, list)) and len(canonical({**output, "results": output["results"][:1]})) + 1 > args.max_bytes:
        output["primer"] = f"omitted: byte cap; raise --max-bytes above {args.max_bytes}"
    while output["results"] and len(canonical(output)) + 1 > args.max_bytes:
        output["results"].pop()
        finish()
    if len(canonical(output)) + 1 > args.max_bytes:
        output = {"schema_version": 1, "status": "partial", "report": report, "results": [],
                  "results_truncated": "Metadata exceeds budget; increase --max-bytes",
                  "omissions": {"envelope_omitted": True},
                  "delivery": {"complete": False, "reason": "metadata_budget", "total": len(all_rows)},
                  "next": {"action": "show", "ref": report, "hint": "Increase --max-bytes"}}
    print(render_table(output, args.fields) if args.format == "table" else canonical(output).decode())


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
        if "text" in document:
            sys.stdout.write(document["text"])
            return 0
        if args.action == "history":
            from helper.target_history import deliver as deliver_history
            deliver_history(document, args)
        else:
            deliver(store, document, args)
        return 0
    except (ValueError, OSError, RecursionError) as error:
        print(json.dumps({"status": "error", "results": [], "error": str(error)[:4000]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
