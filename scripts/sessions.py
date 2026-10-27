#!/usr/bin/env python3
"""Explore original coding-session JSONL; all derived data is invocation-local."""
import argparse
import json
import sys

from helper import actions
from helper.envelope import emit
from helper.graph_output import check_fields, render_table
from helper.projection import FIELDS
from helper.profiles import PROFILES


# Commands that return result rows take the same paging, projection and repository flags as remember.py.
ROWS = ("overview", "projects", "search", "select", "duplicates", "recurrence", "records", "edges", "context", "cite")
SHARED = ("--format", "--fields", "--repo", "--limit")


class Parser(argparse.ArgumentParser):
    """Unknown options are reported by the chosen subcommand, with that subcommand's usage."""

    def parse_args(self, args=None, namespace=None):
        args, extra = self.parse_known_args(args, namespace)
        if extra:
            choices = next((a.choices for a in self._actions if isinstance(a, argparse._SubParsersAction)), {})
            (choices.get(getattr(args, "action", None)) or self).error("unrecognized arguments: " + " ".join(extra))
        return args


COLUMNS = ("harness", "session", "kind", "role", "turn", "tool", "path", "command", "outcome", "exit_code", "text", "cite")


def table(action, fields):
    """Result rows of every harness group as one table, each tagged with its harness."""
    def render(result):
        flat = isinstance(result.get("results"), list) or not isinstance(result.get("harnesses"), list)
        groups = [result] if flat else result["harnesses"]
        rows = [{**({"harness": g.get("harness")} if g is not result else {}), **r}
                for g in groups for r in (g.get("results") or []) if isinstance(r, dict)]
        present = list(dict.fromkeys(k for r in rows for k in r))
        known = [k for k in COLUMNS if k in present]
        columns = fields or (known if len(known) >= 3 else [k for k in present if k != "ref" or "cite" not in present])
        delivery = result.get("delivery") or {}
        view = {"status": "fail" if result.get("check") == "fail" else result.get("status", "ok"), "operation": action, "results": rows, "report": "transient",
                "delivery": {"total": result.get("matched", len(rows))}, "next": result.get("next")}
        if delivery.get("complete") is False:
            view["results_truncated"] = "incomplete: " + (", ".join(delivery.get("incomplete_reasons", [])) or "unknown")
        # Hidden tool rows are a count to act on, not an absence of matches.
        hidden = sum(n for g in [result, *(result.get("harnesses") or [])] if isinstance(g, dict) for k, n in (g.get("excluded") or {}).items() if isinstance(n, int))
        if hidden:
            view["next"] = [f"{hidden} tool_call/tool_result rows hidden; add --include-tools", *(view["next"] or [])]
        return render_table(view, columns)
    return render


def project(result, fields):
    """--fields for commands whose rows are built before projection; a selected row keeps its marker."""
    for holder in [result, *(result.get("harnesses") or [])]:
        if isinstance(holder, dict) and isinstance(holder.get("results"), list):
            holder["results"] = [{k: r[k] for k in [*fields, "selected"] if k in r} if isinstance(r, dict) else r
                                 for r in holder["results"]]


def parser():
    p = Parser(description=__doc__)
    sub = p.add_subparsers(dest="action")
    for action in ("overview", "projects", "search", "select", "duplicates", "recurrence", "records", "edges", "context",
                   "payload", "raw", "cite", "describe"):
        q = sub.add_parser(action)
        q.add_argument("--format", choices=("json", "table"), default="json",
                       help="json envelope, or a table of result rows (harness groups flattened)")
        q.add_argument("--fields", default=("session,harness,kind,role,turn,tool,path,command,outcome,exit_code,text,ref"
                                            if action in ("select", "duplicates") else None),
                       help="Comma-separated result columns" + ("; event fields, read before --sql"
                                                                if action in ("search", "select", "duplicates") else ""))
        if action in ROWS:
            q.add_argument("--repo", help="Repository worktree: recordings filter when --project is absent; "
                                          "select git/files tables; cite commit checks. Defaults to the invocation directory")
        if action == "cite":
            q.add_argument("--limit", type=int, default=None, help="Rows per page (all)")
            q.add_argument("--offset", type=int, default=0)
        # describe is one static schema document, read whole once; recurrence rows carry example refs.
        q.add_argument("--max-bytes", type=int, default=16384 if action in ("describe", "recurrence") else 8192,
                       help="Total JSON response byte budget (shared by every harness group)")
        if action == "describe":
            continue
        q.add_argument("--harness", choices=("auto", *PROFILES), default="auto",
                       help="Force one profile; with default recordings, read only that harness's archives")
        q.add_argument("--max-record-bytes", type=int, default=2 * 1024 * 1024)
        if action == "cite":
            q.add_argument("files", nargs="+", help="Written reports whose citations are checked; - reads stdin")
            continue
        if action in ("overview", "search", "select", "duplicates", "recurrence", "records", "edges", "context"):
            q.add_argument("files", nargs="*", help="JSONL files or directories; defaults to every harness in settings.json")
            q.add_argument("--project", default=None,
                           help="Match recorded cwd exactly; defaults to the invocation directory; '' disables")
            q.add_argument("--max-files", type=int, default=None,
                           help="Per harness; defaults to 25 with a project filter, else 8")
            q.add_argument("--max-candidates", type=int, default=None,
                           help="Per harness/source group; defaults to 20000 with a project filter, else 1000")
            q.add_argument("--max-scan-bytes", type=int, default=64 * 1024 * 1024, help="Per harness")
            q.add_argument("--max-events", type=int, default=25000, help="Projection limit per harness")
            q.add_argument("--max-text-bytes", type=int, default=None,
                           help="Full text stored per harness; defaults to the selected recordings' size, "
                                "between 8 MiB and 32 MiB")
            q.add_argument("--preview-chars", type=int, default=300)
            q.add_argument("--text", action="append", default=[], help="Case-insensitive literal; repeated terms are OR")
            q.add_argument("--role", choices=("user", "assistant", "tool"))
            if action in ("overview", "search", "select", "duplicates"):
                q.add_argument("--human", action="store_true",
                               help="Human requests only: user_message rows with origin=human outside "
                                    "subagent and forked recordings (parent_session absent)")
            q.add_argument("--kind", action="append", default=[])
            q.add_argument("--path", help="Normalized recorded activity path prefix")
            q.add_argument("--limit", type=int, default={"select": None, "duplicates": 20, "context": None}.get(action, 8),
                           help="Rows per page; select defaults to 30, or with --sql to the query's own LIMIT "
                                "(capped at 1000); context defaults to the selected event +/- --radius")
            q.add_argument("--offset", type=int, default=None if action == "context" else 0,
                           help="Page each harness's matched rows before response budgeting; "
                                "context pages default to the window around the selected event")
        if action == "projects":
            q.add_argument("files", nargs="*", help="JSONL files or directories; defaults to every harness in settings.json")
            q.add_argument("--max-candidates", type=int, default=1000, help="Newest headers read per harness/source group")
            q.add_argument("--limit", type=int, default=30)
            q.add_argument("--offset", type=int, default=0)
            q.set_defaults(project="", max_files=1)
        if action in ("context", "payload", "raw"):
            q.add_argument("--ref", required=True, help="A row's cite token (id@line#hash) or its JSON ref object")
        if action == "context":
            q.add_argument("--mode", choices=("turn", "exchange", "nearby", "request"), default="turn")
            q.add_argument("--radius", type=int, default=3, help="Neighbors each side of the selected event (3)")
            q.add_argument("--compare-ref", help="Second request ref for changed-components comparison")
            q.add_argument("--tail", type=int, metavar="N", help="Page the last N events of the turn or window")
        if action in ("payload", "raw"):
            q.add_argument("--start", type=int, default=0, help="Character offset for payload; byte offset for raw")
            q.add_argument("--chars", type=int, default=1000)
            q.add_argument("--tail", action="store_true")
            q.add_argument("--find", help="Literal search; show a matching span")
        if action == "payload":
            q.add_argument("--pointer", help="JSON pointer into the original record; defaults to ref.pointer")
        if action == "records":
            q.add_argument("--native-type", action="append", default=[], help="Filter raw native record type")
            q.add_argument("--parse-status", choices=("ok", "malformed_record", "oversized_record"),
                           help="Filter decoded, malformed, or oversized raw records")
        if action == "search":
            q.add_argument("--include-tools", action="store_true",
                           help="Keep tool_call and tool_result rows; hidden by default unless --kind or --role tool asks")
            q.add_argument("--order", choices=("rank", "time"), default="rank",
                           help="rank: human requests, then assistant decisions, then the rest; time: recorded order")
            q.add_argument("--recordings", action="store_true",
                           help="Include each harness group's recording list (overview always has it)")
        if action in ("search", "select", "duplicates"):
            q.add_argument("--field", action="append", default=[], help="Additional alias=/raw/record/pointer")
        if action == "recurrence":
            q.add_argument("--by", choices=("error", "command"), default="error",
                           help="Group failed results by normalized error line or by command; --text groups by term")
            q.add_argument("--min-sessions", type=int, default=2,
                           help="Independent sessions a derived group needs; --text terms are always shown")
            q.add_argument("--examples", type=int, default=5, help="Example refs per group, one per independent session")
            q.add_argument("--include-noise", action="store_true",
                           help="Keep tool and environment failures (file read/edit errors, missing paths, "
                                "absent commands); hidden by default unless --text names terms")
        if action == "duplicates":
            q.add_argument("--span", type=int, default=7, help="Normalized code lines per compared window")
            q.add_argument("--min-chars", type=int, default=200, help="Skip windows shorter than this many characters")
        if action in ("select", "duplicates"):
            q.add_argument("--sql", help="Read-only SELECT or CTE over every matched row (view rows, all fields); "
                                         "--limit, when given, caps the query output")
            q.add_argument("--max-query-rows", type=int, default=100000)
            q.add_argument("--query-seconds", type=float, default=30.0, help="Deadline for one --sql query")
            q.add_argument("--memory-limit", default="512MB", help="DuckDB memory budget, such as 256MB")
            q.add_argument("--type", action="append", default=[], metavar="[VIEW.]COLUMN=TYPE",
                           help="Declare a column type: BIGINT, DOUBLE, VARCHAR, BOOLEAN, or JSON")
            q.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                           help="Bind a named query parameter ($name)")
            q.add_argument("--full-text", action="store_true",
                           help="Re-read budget-omitted text for matched rows before --sql")
            q.add_argument("--include", default="",
                           help="Comma list of repository tables: git (commit_links, commits, changes), "
                                "files, symbols (code_items; implies files), content (files.content), "
                                "edits (edit_hunks, edit_symbols; implies symbols)")
            q.add_argument("--max-commits", type=int, default=2000, help="Commits read from HEAD history")
            q.add_argument("--follow", metavar="PATH", help="Limit commits/changes to one path's history across renames")
            q.add_argument("--max-repo-files", type=int, default=20000, help="Worktree paths in the files table")
            q.add_argument("--max-file-bytes", type=int, default=1024 * 1024,
                           help="Larger files are listed but not read, hashed or parsed")
            q.add_argument("--root-map", action="append", default=[], metavar="OLD=NEW",
                           help="Read paths recorded under OLD as NEW for repo_path and edit tables (a fork, "
                                "rename or move); with a project filter, recordings rooted at OLD are also read")
    return p


def main():
    p = parser()
    # Bare invocation is the bounded overview over local recordings.
    args = p.parse_args(sys.argv[1:] or ["overview"])
    for key, value in vars(args).items():
        if key in ("max_bytes", "max_record_bytes", "max_scan_bytes", "max_files", "max_candidates",
                   "max_events", "max_text_bytes", "preview_chars", "limit", "chars", "max_query_rows",
                   "query_seconds", "max_commits", "max_repo_files", "max_file_bytes", "min_sessions", "examples") and value is not None and value <= 0:
            p.error(f"--{key.replace('_', '-')} must be positive")
        if key == "tail" and args.action == "context" and value is not None and value <= 0:
            p.error("--tail must be positive")
        if key in ("offset", "start", "radius") and value is not None and value < 0:
            p.error(f"--{key} must be nonnegative")
    if args.action == "context" and args.tail is not None and args.offset is not None:
        p.error("--tail and --offset both choose the page; use one")
    for mapping in getattr(args, "root_map", []):
        old, sep, new = mapping.partition("=")
        if not sep or not old or not new:
            p.error("--root-map takes OLD=NEW")
    if args.action == "select" and args.limit is None and not args.sql:
        args.limit = 30
    if args.action == "context" and args.limit is None:
        args.limit = 8 if args.mode == "request" else 2 * args.radius + 1
    # remember.py history calls the same column date.
    fields = [{"date": "timestamp"}.get(f.strip(), f.strip()) for f in args.fields.split(",") if f.strip()] if args.fields else None
    if fields:
        args.fields = ",".join(fields)
    try:
        if args.action in ("search", "select", "duplicates"):
            # Event fields are projected before rows exist; --field aliases and --include columns join them.
            include = {part.strip() for part in getattr(args, "include", "").split(",")}
            check_fields(fields, None, [*FIELDS, "cite", *(d.partition("=")[0] for d in args.field),
                                        *(["git"] if "git" in include else []),
                                        *(["repo_path", "file"] if include & {"files", "symbols", "edits"} else [])])
        result = actions.run(args)
        if fields and args.action not in ("search", "select", "duplicates"):
            check_fields(fields, [r for h in [result, *(result.get("harnesses") or [])] if isinstance(h, dict)
                                  for r in (h.get("results") if isinstance(h.get("results"), list) else ())])
            project(result, fields)
        if args.action == "cite" and (args.offset or args.limit):
            rows = result["results"]
            result["results"] = rows[args.offset:args.offset + args.limit if args.limit else None]
            result.setdefault("omissions", {})["selection"] = len(rows) - len(result["results"])
        emit(result, args.max_bytes, table(args.action, fields) if args.format == "table" else None)
    except (ValueError, OSError, json.JSONDecodeError) as error:
        emit({"status": "error", "results": [{"error": str(error)[:500]}]}, max(1024, args.max_bytes))
        return 2
    # A citation check that found problems fails like a test, after its rows are printed.
    return 1 if result.get("check") == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
