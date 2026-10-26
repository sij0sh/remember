"""Explicit, transient history retrieval from one resolved source target.

Git and recordings are independent lanes. Only exact normalized paths, witnessed
renames and caller-supplied relocation/split associations cross their boundary.
"""
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from . import commit_links, edit_hunks, git_file_history
from .actions import lineages
from .discovery import candidate_files
from .enrichment import parse_root_map, remap
from .git_resolve import GitResolver
from .options import DiscoveryOptions, ScanOptions, command_line
from .projection import clean, next_for
from .reader import scan_file
from .records import RecordCache


def continuation(graph, node, store):
    target = graph.public(node)["ref"]
    argv = [sys.executable, str(Path(__file__).resolve().parents[1] / "remember.py"),
            "history", target, "--repo", store.root, "--cache", str(store.path.parent)]
    return {"next": {"action": "history", "target": target, "snapshot": graph.snapshot,
                     "repo": store.root, "argv": command_line(argv)}}


def attach(document, graph, targets, store):
    """Continuation metadata only: never discovers or reads recordings."""
    found = []
    for target in targets:
        hits = graph.resolve(target)
        if len(hits) == 1 and graph.nodes[hits[0]]["kind"] in ("file", "symbol"):
            found.append(continuation(graph, graph.nodes[hits[0]], store)["next"])
    if found:
        document["history"] = {"next": found[0] if len(found) == 1 else found}
    for row in document.get("results", []):
        if row.get("id") in graph.nodes and row.get("kind") in ("file", "symbol"):
            row["history"] = continuation(graph, graph.nodes[row["id"]], store)
    return document


def relative(value):
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value or value == ".":
        raise ValueError("Historical paths must be repository-relative and contain no '..'")
    return path.as_posix().removeprefix("./")


def resolve(args, store, get_graph):
    """Pinned refs reuse their graph. Exact file paths need no repository reparse."""
    target = args.target
    pin = target.split("/", 1)[0] if target.startswith("snapshot:") else args.snapshot
    if pin and args.snapshot and pin != args.snapshot:
        raise ValueError("Target ref and --snapshot disagree")
    if not pin and "::" not in target:
        path = Path(store.root, target)
        if path.is_file():
            path = relative(target)
            return {"kind": "file", "path": path, "resolution": "current_file" if Path(store.root, path).is_file()
                    else "historical_path", "snapshot": None}, None
    path, sep, _ = target.partition("::")
    if not pin and sep and Path(store.root, path).is_file():
        # Reuse graph identity/resolution, but extract only the explicitly named file.
        from .graph_build import Builder
        from .graph_queries import Graph
        from .graph_store import read_file
        path = relative(path)
        row, _ = read_file(Path(store.root), path, args.max_file_bytes, args.max_file_bytes)
        source = {"root": store.root, "files": [row]}
        builder = Builder(source)
        builder.structures()
        graph = Graph(None, {"source": source, "nodes": list(builder.nodes.values())})
    else:
        graph = get_graph(store, args, pin)
    def public(node):
        return graph.public(node) if graph.snapshot else dict(node)
    hits = graph.resolve(target)
    if len(hits) != 1:
        if not hits and "::" not in target and not target.startswith(("snapshot:", "node:")):
            return {"kind": "file", "path": relative(target), "resolution": "historical_path",
                    "snapshot": graph.snapshot}, graph
        return {"resolution": "ambiguous" if hits else "unresolved", "target": target,
                "candidates": [public(graph.nodes[n]) for n in hits]}, graph
    node = public(graph.nodes[hits[0]])
    if node["kind"] not in ("file", "symbol"):
        raise ValueError("History requires a file or symbol target")
    return {**node, "snapshot": graph.snapshot, "resolution": "resolved"}, graph


class TargetRepository:
    """Minimal edit_hunks adapter: only selected events and pinned target text."""
    def __init__(self, target, graph, root, aliases, roots, root_map, repo_id, max_bytes):
        self.target, self.aliases, self.roots, self.root_map = target, aliases, roots, root_map
        self.repo_id = repo_id
        self.text, self.items = None, []
        self.state = "source_unavailable"
        if graph:
            row = next((r for r in graph.body["source"]["files"] if r["path"] == target["path"]), {})
            self.text = row.get("text")
            self.items = [{**n, "kind": n.get("symbol_kind"), "parent": None}
                          for n in graph.nodes.values() if n["kind"] == "symbol" and n["path"] == target["path"]]
            self.state = "pinned_source" if graph.snapshot else "invocation_source"
        else:
            try:
                with open(Path(root, target["path"]), "rb") as stream:
                    data = stream.read(max_bytes + 1)
                if len(data) <= max_bytes:
                    self.text = data.decode("utf-8", "replace")
                    self.state = "current_source"
            except OSError:
                pass

    def absolute(self, event):
        path = event.get("path")
        if not path or event.get("path_location") not in ("repository", "external"):
            return None
        if event.get("path_location") == "repository":
            root = self.roots.get(event.get("recording_id"))
            if not root:
                return None
            path = os.path.join(root, path)
        return os.path.normpath(remap(path, self.root_map)[0])

    def repo_path(self, event):
        return self.target["path"] if self.absolute(event) in self.aliases else None

    def current_lines(self, path):
        return (edit_hunks.split_lines(self.text), None) if path and self.text is not None else (None, self.state)

    def has_items(self, path):
        return bool(path and self.items)

    def items_for(self, path):
        return self.items if path else []


def scan(args, root):
    scope = {"duplicates_skipped": 0}
    options = DiscoveryOptions(files=tuple(args.recordings), project=root,
                               also_roots=tuple(args.project_root), max_files=args.max_recordings,
                               max_candidates=args.max_candidates)
    groups = candidate_files(options, scope)
    recordings, coverage = [], []
    for harness, files, counters in groups:
        for file in files:
            if counters["bytes_scanned"] >= args.max_scan_bytes or counters.get("event_limit_reached"):
                counters["scan_complete"] = False
                break
            recordings.append(scan_file(file, ScanOptions(harness=harness, max_scan_bytes=args.max_scan_bytes,
                                                          max_events=args.max_events), counters))
            counters["files_scanned"] += 1
        coverage.append({"harness": harness, **counters})
    keys = lineages(recordings)
    excluded = set(args.exclude_session)
    excluded.update(v for v in (os.environ.get("CODEX_THREAD_ID"), os.environ.get("PI_SESSION_ID"),
                                os.environ.get("CLAUDE_SESSION_ID")) if v)
    kept = []
    for recording in recordings:
        if {recording["session"], recording["file"], keys[recording["file"]]} & excluded:
            continue
        for event in recording["events"]:
            event["independent_session"] = keys[recording["file"]]
        kept.append(recording)
    partial = not scope["discovery_complete"] or any(not g["scan_complete"] or not g["text_complete"] for g in coverage)
    return kept, {**scope, "groups": coverage, "state": "partial" if partial else "searched",
                  "excluded_sessions": sorted(excluded), "recordings_excluded": len(recordings)-len(kept),
                  "current_session_exclusion": "configured" if excluded else "unknown; pass --exclude-session",
                  "independent_sessions": len({keys[r["file"]] for r in kept})}


def literal(text, value):
    return bool(value and re.search(r"(?<![\w./\\])" + re.escape(value) + r"(?![\w./\\])", text))


def run(args, store, get_graph):
    for value in args.root_map:
        old, sep, new = value.partition("=")
        if not sep or not old or not new:
            raise ValueError("--root-map requires OLD=NEW with nonempty paths")
    args.project_root = [str(Path(p).expanduser().resolve()) for p in args.project_root]
    target, graph = resolve(args, store, get_graph)
    result = {"schema_version": 1, "operation": "history", "status": "ok", "target": target,
              "scope": {"repo": store.root, "retention": "transient", "history": "live", "symbol_identity": "not inferred across versions"},
              "coverage": {}, "results": []}
    if target["resolution"] in ("ambiguous", "unresolved"):
        result["status"] = target["resolution"]
        return result
    resolver = GitResolver()
    current_repo = resolver.repo(store.root)
    lanes = [(current_repo, os.path.relpath(Path(store.root, target["path"]), current_repo).replace(os.sep, "/")
              if current_repo else target["path"], "current_repository")]
    for association in args.history_repo:
        repo, sep, prefix = association.partition("=")
        if not sep:
            raise ValueError("--history-repo requires REPO=OLD_PREFIX (prefix may be empty)")
        repo = str(Path(repo).expanduser().resolve())
        if prefix:
            prefix = relative(prefix)
        lanes.append((repo, relative(str(Path(prefix, target["path"]))), "explicit_repository_association"))
    aliases = {os.path.normpath(str(Path(store.root, target["path"]))): {"basis": "current_path", "path": target["path"]}}
    commits, git_coverage = {}, []
    for repo, path, basis in lanes:
        if not repo:
            git_coverage.append({"state": "unavailable", "reason": "not a Git repository", "path": path})
            continue
        found, changes, coverage = git_file_history.history(resolver, repo, args.max_commits, path)
        git_coverage.append({"repo": repo, "path": path, "basis": basis, **coverage,
                             "state": "unavailable" if coverage.get("error") else "searched" if coverage["complete"] else "partial"})
        aliases[os.path.normpath(str(Path(repo, path)))] = {"basis": basis, "repo": repo, "path": path}
        for change in changes:
            if change["status"] == "renamed":
                for old in (change["old_path"], change["new_path"]):
                    aliases[os.path.normpath(str(Path(repo, old)))] = {"basis": "git_rename", "repo": repo,
                                                                 "path": old, "sha": change["sha"]}
        for commit in found:
            commits[(commit["repo_id"], commit["sha"])] = commit
            result["results"].append({"category": "git_change", "basis": "symbol -> file" if target["kind"] == "symbol" else "file_history",
                                      "repo": repo, "ref": commit["sha"], "subject": commit["subject"],
                                      "committed_at": commit["committed_at"],
                                      "changes": [c for c in changes if c["sha"] == commit["sha"]],
                                      "next": {"argv": command_line(["git", "-C", repo, "show", commit["sha"], "--", path])}})
    result["coverage"]["git"] = git_coverage
    result["scope"]["aliases"] = [{"absolute": k, **v} for k, v in aliases.items()]
    if args.no_sessions:
        result["coverage"]["sessions"] = {"state": "not_searched"}
        return result
    try:
        recordings, coverage = scan(args, store.root)
    except (ValueError, OSError) as error:
        result["coverage"]["sessions"] = {"state": "unavailable", "reason": str(error)}
        return result
    result["coverage"]["sessions"] = coverage
    roots = {r["file"]: r.get("root") for r in recordings}
    repository = TargetRepository(target, graph, store.root, aliases, roots, parse_root_map(args.root_map),
                                  resolver.identity(current_repo), args.max_file_bytes)
    selected = [{**r, "events": [e for e in r["events"] if repository.repo_path(e)]} for r in recordings]
    cache = RecordCache(2 * 1024 * 1024)
    hunks, symbols, mapping = edit_hunks.derive(selected, cache, repository)
    mapping["source_basis"] = repository.state
    result["coverage"]["mapping"] = mapping
    by_event, hunks_by_event = {}, {}
    for hunk in hunks:
        hunks_by_event.setdefault((hunk["recording_id"], hunk["event_id"]), []).append(hunk)
    for symbol in symbols:
        if target["kind"] == "symbol" and symbol["qualname"] == target["qualname"]:
            by_event.setdefault((symbol["recording_id"], symbol["event_id"]), []).append(symbol)
    for recording in recordings:
        mention_paths = []
        recorded_root = recording.get("root")
        if recorded_root:
            recorded_root = os.path.normpath(remap(recorded_root, repository.root_map)[0])
            for absolute in aliases:
                try:
                    if os.path.commonpath([recorded_root, absolute]) == recorded_root:
                        mention_paths.extend((absolute, os.path.relpath(absolute, recorded_root).replace(os.sep, "/")))
                except ValueError:  # different drives cannot share a relative path
                    pass
        for event in recording["events"]:
            direct = repository.repo_path(event)
            text = event.get("text", "")
            # Text echoes from tools, summaries, forks and copied result envelopes are not past support.
            mention = (event["kind"] in ("user_message", "assistant_message") and not event.get("parent_session")
                       and event.get("origin") in ("human", "assistant")
                       and not any(marker in text for marker in ('"schema_version"', '"recording_id"', '"payload_pointer"'))
                       and any(literal(text, path) for path in mention_paths)
                       and (target["kind"] != "symbol" or literal(text, target["qualname"])))
            if not direct and not mention:
                continue
            match = by_event.get((event["recording_id"], event["event_id"]), [])
            basis = ("recorded_definition_at_edit" if any(s["basis"] == "at_edit" for s in match)
                     else "recorded_text_matches_target_source" if match else "symbol -> file" if direct and target["kind"] == "symbol"
                     else "exact_file_activity" if direct else "scoped_literal_mention")
            row = clean(event, SimpleNamespace(preview_chars=300))
            result["results"].append({"category": "recorded_activity" if direct else "text_mention", "basis": basis,
                                      "evidence": row, "symbols": match,
                                      "hunks": hunks_by_event.get((event["recording_id"], event["event_id"]), []),
                                      "ref": event["ref"], "next": next_for(event)})
    # Witnesses are joined by repository and SHA, never by timestamp or nearby edit.
    witnesses = commit_links.witnesses(recordings, resolver, cache)
    result["coverage"]["witnesses"] = {"scanned": len(witnesses),
        "unresolved": sum(w["confidence"] == "unresolved" for w in witnesses),
        "matched_target_commits": sum((w["repo_id"], w["sha"]) in commits for w in witnesses),
        "basis": "event to commit only; does not prove earlier edits entered the commit"}
    for witness in witnesses:
        if (witness["repo_id"], witness["sha"]) in commits:
            result["results"].append({"category": "commit_witness", "basis": witness["basis"], "evidence": witness,
                                      "ref": witness["ref"], "next": next_for({**witness, "kind": "tool_call"})})
    result["results"].sort(key=lambda r: (0 if r["basis"] in ("recorded_definition_at_edit", "recorded_text_matches_target_source")
                                          else 1 if r["category"] == "recorded_activity" and r["evidence"].get("operation") == "file_edit"
                                          else 2 if r["category"] == "git_change" else 3 if r["category"] == "commit_witness"
                                          else 4 if r["category"] == "recorded_activity" else 5))
    return result


def deliver(document, args):
    """Bound output without persisting transcript text in graph reports."""
    from .graph_store import canonical
    from .graph_output import project_rows, render_table
    rows = document["results"]
    total = len(rows)
    document["results"] = project_rows(rows[args.offset:args.offset + args.limit], getattr(args, "fields", None))
    partial = any(c.get("state") in ("partial", "unavailable") for c in document.get("coverage", {}).get("git", []))
    partial |= document.get("coverage", {}).get("sessions", {}).get("state") in ("partial", "unavailable")
    def finish():
        following = args.offset + len(document["results"])
        document["delivery"] = {"total": total, "returned": len(document["results"]), "offset": args.offset,
                                "complete": following >= total}
        document["next"] = {"action": "history", "target": args.target, "offset": following,
                            "hint": "Repeat the same options; history is live. Increase --max-bytes if no row fits."} if following < total else None
        if following < total:
            document["results_truncated"] = f"{len(document['results'])} of {total} rows shown; --offset {following} or raise --limit / --max-bytes"
        if partial or following < total:
            document["status"] = "partial"
    finish()
    while document["results"] and len(canonical(document)) + 1 > args.max_bytes:
        document["results"].pop()
        finish()
    if len(canonical(document)) + 1 > args.max_bytes:
        document = {"operation": "history", "status": "partial", "results": [], "delivery": {"complete": False},
                    "next": {"action": "history", "target": args.target, "hint": "Increase --max-bytes; metadata exceeds budget."}}
    print(render_table(document, getattr(args, "fields", None)) if getattr(args, "format", None) == "table" else canonical(document).decode())
