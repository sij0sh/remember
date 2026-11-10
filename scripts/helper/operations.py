"""Declarative operation registry: one definition per CLI operation drives parser, help, routing and delivery.

Each argument is defined once with the operations it applies to; `graph_parser()` builds the CLI from it.
"""
import argparse
from dataclasses import dataclass

from .whole_repo import MAX_LINES


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


@dataclass(frozen=True)
class Operation:
    name: str
    help: str
    route: str           # runtime | graph | describe
    summary: str = ""    # common-interface description, when it differs from help


OPERATIONS = {op.name: op for op in (
    Operation("map", "List source files, directories or symbols and graph coverage (default).",
              "graph", "Orient in current source."),
    Operation("find", "Locate names, paths and literal source occurrences; matches are candidates.",
              "graph", "Locate a concrete anchor."),
    Operation("show", "Expand source, nodes, edges, session or commit evidence; run a continuation.",
              "runtime", "Expand evidence or execute a returned continuation."),
    Operation("query", "Run one read-only SQLite SELECT/CTE over nodes, edges, files and gaps.",
              "runtime", "Query built-in and custom tables; dependencies load automatically."),
    Operation("describe", "Describe commands, predicates and SQL tables without scanning source.", "describe"),
    Operation("activity", "Chronological recorded operations for one file or symbol target, with outcomes and witnesses.",
              "runtime", "See what was read, written, run and committed for a target."),
    Operation("trace", "Reconstruct the branch-aware episode around a request, failure, checkpoint or commit.",
              "runtime", "Reconstruct an episode: chronology, coverage gaps, task observations."),
    Operation("signals", "Recorded signals for one file or symbol: failed exchanges, corrections, checkpoint gaps, repeated reads.",
              "runtime", "Target-scoped leads: failed exchanges, corrections, checkpoint gaps, repeated reads."),
    Operation("investigate", "Four evidence views for a goal: pitfalls, invariants, task state, decisions.",
              "runtime", "Before building or resuming: pitfalls, invariants, task state, decisions."),
    Operation("inspect", "Collect source, consumers, dependencies and relevant history.",
              "runtime", "Collect source, consumers, dependencies and relevant history."),
)}

# Operations whose subcommand help shows the short common-interface summary.
VIEWS = ("investigate", "inspect", "activity", "signals", "trace")
COMMON = {n: op.summary for n, op in OPERATIONS.items() if op.summary}


@dataclass(frozen=True)
class Arg:
    flags: tuple
    options: dict
    ops: frozenset | None   # None: every operation

    def applies(self, name):
        return self.ops is None or name in self.ops

    def resolved(self, name):
        return dict(self.options)


def arg(*flags, ops=None, **options):
    return Arg(flags, options, frozenset((ops,) if isinstance(ops, str) else ops) if ops is not None else None)


# Shared remember.py options; each subcommand repeats them so they work before or after the verb.
GRAPH_SHARED = (
    arg("--repo", help="Source root, including an ordinary directory; default current directory"),
    arg("--cache", help="Cache parent outside source tree; default $XDG_CACHE_HOME/remember"),
    arg("--snapshot", help="Use the current cached snapshot ID instead of capturing source; superseded IDs fail"),
    arg("--exclude", action="append", help="Exclude relative-path/component glob; repeatable"),
    arg("--max-files", type=positive, help="Capture file count limit (20000)"),
    arg("--max-file-bytes", type=positive, help="Per-file read limit (1 MiB)"),
    arg("--max-total-bytes", type=positive, help="Total captured bytes (32 MiB)"),
    arg("--limit", type=positive, help="Rows per page (query: 200, others: 30); also raises query retention"),
    arg("--offset", type=nonnegative, help="Delivered row offset (0)"),
    arg("--max-bytes", type=positive, help="JSON response budget, at least 2048 (12000)"),
    arg("--format", choices=("json", "table"), help="Result format; bare map defaults to text"),
    arg("--fields", help="Comma-separated result columns"),
    arg("--verbose", action="store_true",
        help="Full rows: ids, keys, digests, witness endpoints"),
)

TARGET_HISTORY = ("inspect",)
ACTIVITY = ("activity", "signals")
SIGNALS = ("signals",)
TRACE = ("trace",)
INVESTIGATE = ("investigate",)
GRAPH_ARGS = (
    arg("--kind", ops=("map", "find"), action="append", choices=("file", "directory", "symbol", "external", "occurrence")),
    arg("path", ops="map", nargs="?", help="Limit rows to this file, directory or symbol's file"),
    arg("--whole-repo-lines", ops="map", type=nonnegative, default="MAX_LINES",
        help="Bare map prints every source file as text at or under this many lines; 0 disables"),
    arg("--whole", ops="map", action="store_true", help="Bare map: print whole source up to {MAX_BYTES} bytes (default cap {TEXT_BYTES})"),
    arg("--primer", ops="map", dest="primer", action="store_true", default=None,
        help="Include the orientation packet in JSON map output; bare map text includes it by default"),
    arg("--no-primer", ops="map", dest="primer", action="store_false", help="Disable the orientation packet on bare map text"),
    arg("--primer-bytes", ops="map", type=positive, default=2048, help="Text orientation byte budget (2048)"),
    arg("--recordings", ops="map", action="append", default=[], help="Recording file or directory for orientation; repeatable"),
    arg("--project-root", ops="map", action="append", default=[], help="Additional recording project root; repeatable"),
    arg("--exclude-session", ops="map", action="append", default=[], help="Exclude session ID or recording path, including its lineage"),
    arg("text", ops="find", nargs="?"),
    arg("--word", ops="find", action="store_true", help="Match whole words only"),
    arg("--case-sensitive", ops="find", action="store_true", help="Match case exactly (default ignores case)"),
    arg("--regex", ops="find", default=False, help="Python regex PATTERN; supply instead of TEXT"),
    arg("--path", ops="find", action="append", help="Include result path glob or directory; repeatable"),
    arg("--not-path", ops="find", action="append", default=[], help="Exclude result path glob or directory; repeatable"),
    arg("--count-by", ops="find", choices=("path",), help="Count matching result rows per path"),
    arg("target", ops=TARGET_HISTORY),
    arg("target", ops=ACTIVITY, help="Repository-relative path, path::qualified.name, or returned node ref"),
    arg("ref", ops=TRACE, help="Recorded event JSON ref, cite token, or full commit SHA (optional git: prefix)"),
    arg("--recordings", ops=TARGET_HISTORY + ACTIVITY + TRACE, action="append", default=[], help="Recording file/directory; repeatable"),
    arg("--project-root", ops=TARGET_HISTORY + ACTIVITY + TRACE, action="append", default=[], help="Additional historical recording project root"),
    arg("--history-repo", ops=TARGET_HISTORY, action="append", default=[], help="Explicit old Git repository and path prefix: REPO=PREFIX"),
    arg("--root-map", ops=TARGET_HISTORY + ACTIVITY + TRACE, action="append", default=[], help="Rewrite recorded path prefix OLD=NEW; discovery uses --project-root"),
    arg("--exclude-session", ops=TARGET_HISTORY + ACTIVITY + TRACE, action="append", default=[], help="Exclude session ID or recording path, including its lineage"),
    arg("--no-sessions", ops=TARGET_HISTORY, action="store_true"),
    arg("--max-commits", ops=TARGET_HISTORY + TRACE, type=positive, default=30),
    arg("--max-recordings", ops=TARGET_HISTORY + ACTIVITY + TRACE, type=positive, default=25, help="Per-harness recording limit"),
    arg("--max-candidates", ops=TARGET_HISTORY + ACTIVITY + TRACE, type=positive, default=20000,
        help="Per-harness discovery header limit (20000)"),
    arg("--max-scan-bytes", ops=TARGET_HISTORY + ACTIVITY + TRACE, type=positive, default=64 * 1024 * 1024, help="Per-harness scan byte limit"),
    arg("--max-events", ops=TARGET_HISTORY + ACTIVITY + TRACE, type=positive, default=25000, help="Per-harness event limit"),
    arg("ref", ops="show", help="Returned snapshot/source/node/edge reference"),
    arg("--start-line", ops="show", type=positive, default=1),
    arg("--lines", ops="show", type=positive, default=80),
    arg("--param", ops="query", action="append", default=[], help="Bind NAME=VALUE to :NAME"),
    arg("--max-query-rows", ops="query", type=positive),
    arg("--seconds", ops="query", type=positive, default=5),
    arg("sql_text", ops="query", nargs="?", help="Read-only SQLite SELECT/CTE"),
    arg("--input", ops="query", action="append", default=[], metavar="NAME=FILE",
        help="Register JSON/JSONL rows (or - for stdin)"),
    arg("--type", ops="query", action="append", default=[], metavar="TABLE.COLUMN=TYPE"),
    arg("--plan", ops="query", action="store_true", help="Validate SQL and show dependencies before loading providers"),
    *(arg("--" + name, ops="query", action="append", default=[])
      for name in ("recordings", "project-root", "root-map", "exclude-session")),
    *(arg("--" + name, ops="query", type=positive)
      for name in ("max-recordings", "max-candidates", "max-scan-bytes", "max-events", "max-commits")),
    arg("table", ops="describe", nargs="?", help="Focused public table schema"),
    arg("--intent", ops="inspect", choices=("change", "bug", "review"), default="change"),
    arg("--plan", ops="inspect", action="store_true", help="Describe retrieval stages without reading source/history"),
    arg("--depth", ops="inspect", type=positive, default=2),
    arg("--max-nodes", ops="inspect", type=positive, default=1000),
    arg("goal", ops=INVESTIGATE, nargs="*", help="Goal words; omit for repository orientation"),
    arg("--path", ops=INVESTIGATE, action="append", default=[], help="Exact repository-relative path (may be historical); repeatable"),
    arg("--error", ops=INVESTIGATE, action="append", default=[], help="Literal error text; repeatable"),
    arg("--session", ops=INVESTIGATE, help="Active task anchor: returned event JSON ref or cite token"),
    arg("--frontier", ops=INVESTIGATE + TARGET_HISTORY, help=argparse.SUPPRESS),
    arg("--plan", ops=INVESTIGATE, action="store_true", help="Describe retrieval steps without loading providers"),
    arg("--no-sessions", ops=INVESTIGATE, action="store_true", help="Git only; views report not_searched"),
    *(arg("--" + name, ops=INVESTIGATE, action="append", default=[])
      for name in ("recordings", "project-root", "root-map", "exclude-session")),
    *(arg("--" + name, ops=INVESTIGATE, type=positive)
      for name in ("max-recordings", "max-candidates", "max-scan-bytes", "max-events", "max-commits")),
    arg("--plan", ops=SIGNALS, action="store_true", help="Describe signal steps without loading providers"),
    arg("--no-sessions", ops=SIGNALS, action="store_true", help="Views report not_searched"),
    arg("--budget", ops=("query", "inspect", "show", "activity", "signals", "trace", "investigate"), choices="POLICIES", default="standard"),
)


def constants():
    """Values owned by other modules, resolved when a parser is built (avoids import cycles)."""
    from .budget import POLICIES
    from .graph_build import PREDICATES
    from .whole_repo import MAX_BYTES, MAX_LINES, TEXT_BYTES
    return {"PREDICATES": tuple(PREDICATES), "POLICIES": tuple(POLICIES),
            "MAX_LINES": MAX_LINES, "MAX_BYTES": MAX_BYTES, "TEXT_BYTES": TEXT_BYTES}


def derive(args):
    """Fill CLI defaults and derived values; the one place both live."""
    args.action = args.action or "map"
    explicit_limit = getattr(args, "limit", None)
    if args.action == "query":
        args.limit = explicit_limit or args.max_query_rows or 200
        args.max_query_rows = max(args.max_query_rows or 1000, args.limit + getattr(args, "offset", 0))
    for key, value in {"repo": ".", "cache": None, "snapshot": None, "exclude": [], "max_files": 20000,
                       "max_file_bytes": 1024*1024, "max_total_bytes": 32*1024*1024,
                       "limit": 30, "offset": 0, "max_bytes": 12000, "verbose": False,
                       "format": None, "fields": None, "path": None, "kind": None,
                       "whole_repo_lines": MAX_LINES, "whole": False}.items():
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
        for key, value in {"primer": None, "primer_bytes": 2048, "recordings": [], "project_root": [],
                           "exclude_session": []}.items():
            if not hasattr(args, key):
                setattr(args, key, value)
        text_mode = not (args.path or args.kind or args.format or args.fields)
        explicit = getattr(args, "primer", None)
        args.primer_explicit = explicit is not None
        args.primer = text_mode if explicit is None else explicit
        if args.primer_bytes < 512:
            raise ValueError("--primer-bytes must be at least 512")
    else:
        args.primer = False
        args.primer_explicit = False
    return args


def add(parser, args, name=None):
    names = constants()
    for item in args:
        if not item.applies(name):
            continue
        options = item.resolved(name)
        for key in ("choices", "default"):
            if isinstance(options.get(key), str) and options[key] in names:
                options[key] = names[options[key]]
        if isinstance(options.get("help"), str) and "{" in options["help"]:
            options["help"] = options["help"].format(**names)
        parser.add_argument(*item.flags, **options)
    return parser


class GraphParser(argparse.ArgumentParser):
    """Unknown options are reported by the chosen subcommand, with that subcommand's usage."""

    def parse_args(self, args=None, namespace=None):
        args, extra = self.parse_known_args(args, namespace)
        if extra:
            choices = next((a.choices for a in self._actions if isinstance(a, argparse._SubParsersAction)), {})
            (choices.get(getattr(args, "action", None)) or self).error("unrecognized arguments: " + " ".join(extra))
        return args


def graph_parser():
    """remember.py: every operation, one parser."""
    shared = add(argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS), GRAPH_SHARED)
    p = GraphParser(description="Remember: investigate, map, find, inspect, trace, activity, signals, show, query.", parents=[shared])
    sub = p.add_subparsers(dest="action")
    for name, op in OPERATIONS.items():
        text = op.summary if name in VIEWS else op.help
        q = sub.add_parser(name, help=text, description=text, parents=[shared])
        add(q, GRAPH_ARGS, name)
    sub.choices["query"].description = COMMON["query"] + " SQL dialect: SQLite."
    return p
