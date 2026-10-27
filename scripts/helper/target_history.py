"""Explicit, transient history retrieval from one resolved source target.

Git and recordings are independent lanes. Only exact normalized paths, witnessed
renames and caller-supplied relocation/split associations cross their boundary.
"""
import bisect
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from . import cite, commit_links, edit_hunks, git_file_history
from .actions import lineages
from .discovery import candidate_files, normalized_root
from .enrichment import parse_root_map, remap
from .git_resolve import GitResolver
from .options import TEXT_BYTES_AUTO_CAP, TEXT_BYTES_DEFAULT, DiscoveryOptions, ScanOptions, command_line
from .projection import clean, human, next_for
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


# A follow-up that opens by rejecting or redirecting the prior work. A lead to expand, not a verdict.
CORRECTION = re.compile(r"^\W*(no\b|nope\b|wrong\b|revert|undo\b|stop\b|actually\b|instead\b|that'?s not|not what|"
                        r"why did you|don'?t\b|do not\b|this (broke|breaks|is broken)|still (fail|broken|wrong|not))"
                        r"|\b(doesn'?t|didn'?t|does not|did not) work\b|\bbroke (it|the)\b|\bregress", re.IGNORECASE)
# Strongest recorded support first; matches the per-row basis labels below.
BASIS_RANK = {"recorded_definition_at_edit": 0, "recorded_text_matches_target_source": 0,
              "exact_file_activity": 1, "symbol -> file": 1, "scoped_literal_mention": 2}
# Pickaxe needs a distinctive literal; short names match unrelated text everywhere.
PICKAXE_MIN = 4


def when(value):
    """ISO-8601 text for a recorded or committed time; epoch seconds or milliseconds are converted."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    return value if isinstance(value, str) else ""


def note(event):
    """A recorded message as a short locator: enough to judge relevance before expanding it."""
    return {"text": " ".join(str(event.get("text") or "").split())[:300], "timestamp": event.get("timestamp"),
            "origin": "human" if human(event) else "delegated", "ref": event["ref"]}


def intent(event, requests, humans, orders):
    """The request that opened the event's turn, the next human message after it, and plain signals."""
    request = event if event["kind"] == "user_message" else requests.get(event.get("turn"))
    after = bisect.bisect_right(orders, event["order"])
    following = humans[after] if after < len(humans) else None
    signals = []
    if event.get("outcome") == "failed":
        signals.append("failed_edit" if event.get("operation") == "file_edit" else "failed_call")
    if following and CORRECTION.search(str(following.get("text") or "")[:300]):
        signals.append("possible_correction")
    out = {"signals": signals}
    if request:
        out["request"] = note(request)
    if following:
        out["followup"] = note(following)
    return out


def refkey(ref):
    return (ref["file"], ref["offset"], ref.get("item", 0))


def tier(row):
    """Intent first: corrected or failed work, then requested changes, then symbol-specific and file commits.

    Reads and delegated requests come after commits: they show attention, not a decision.
    """
    signals = row.get("intent", {}).get("signals")
    if row["category"] in ("recorded_activity", "text_mention"):
        changed = row["category"] == "text_mention" or row["evidence"].get("operation") == "file_edit"
        return 0 if signals else 1 if changed and row["intent"].get("request", {}).get("origin") == "human" else 4
    if row["category"] == "git_change":
        return 2 if row.get("pickaxe") else 3
    return 5


def rank(result):
    """Tier, then basis, then newest first; the sorts are stable."""
    result["results"].sort(key=lambda r: when(r.get("committed_at") or r.get("intent", {}).get("request", {}).get("timestamp")
                                              or r.get("evidence", {}).get("timestamp")), reverse=True)
    result["results"].sort(key=lambda r: (tier(r), BASIS_RANK.get(r["basis"], 3)))
    return result


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
        return normalized_root(remap(path, self.root_map)[0])

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
        # Like sessions.py: store full text for the bytes this group will scan, so a target mention is not cut off.
        size = sum(os.path.getsize(f) for f in files if os.path.exists(f))
        text = max(TEXT_BYTES_DEFAULT, min(size, args.max_scan_bytes, TEXT_BYTES_AUTO_CAP))
        for file in files:
            if counters["bytes_scanned"] >= args.max_scan_bytes or counters.get("event_limit_reached"):
                counters["scan_complete"] = False
                counters["byte_limit_reached"] = counters["bytes_scanned"] >= args.max_scan_bytes
                break
            recordings.append(scan_file(file, ScanOptions(harness=harness, max_scan_bytes=args.max_scan_bytes,
                                                          max_events=args.max_events, max_text_bytes=text), counters))
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
    args.project_root = [str(Path(p).expanduser()) for p in args.project_root]
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
    aliases = {normalized_root(Path(store.root, target["path"])): {"basis": "current_path", "path": target["path"]}}
    commits, git_coverage = {}, []
    for repo, path, basis in lanes:
        if not repo:
            git_coverage.append({"state": "unavailable", "reason": "not a Git repository", "path": path})
            continue
        found, changes, coverage = git_file_history.history(resolver, repo, args.max_commits, path)
        git_coverage.append({"repo": repo, "path": path, "basis": basis, **coverage,
                             "state": "unavailable" if coverage.get("error") else "searched" if coverage["complete"] else "partial"})
        aliases[normalized_root(Path(repo, path))] = {"basis": basis, "repo": repo, "path": path}
        for change in changes:
            if change["status"] == "renamed":
                for old in (change["old_path"], change["new_path"]):
                    aliases[normalized_root(Path(repo, old))] = {"basis": "git_rename", "repo": repo,
                                                                 "path": old, "sha": change["sha"]}
        for commit in found:
            commits[(commit["repo_id"], commit["sha"])] = commit
            result["results"].append({"category": "git_change", "basis": "symbol -> file" if target["kind"] == "symbol" else "file_history",
                                      "repo": repo, "ref": commit["sha"], "subject": commit["subject"],
                                      "committed_at": commit["committed_at"],
                                      "changes": [c for c in changes if c["sha"] == commit["sha"]],
                                      "next": {"argv": command_line(["git", "-C", repo, "show", commit["sha"], "--", path])}})
    name = target.get("name") or str(target.get("qualname") or "").rpartition(".")[2]
    if target["kind"] == "symbol" and current_repo and len(name) >= PICKAXE_MIN:
        # Commits that added or removed the name anywhere: moved definitions and removed callers.
        found, changes, coverage = git_file_history.history(resolver, current_repo, args.max_commits, pickaxe=name)
        git_coverage.append({"repo": current_repo, "pickaxe": name, "basis": "symbol_name_pickaxe", **coverage,
                             "state": "unavailable" if coverage.get("error") else "searched" if coverage["complete"] else "partial"})
        listed = {r["ref"]: r for r in result["results"] if r["category"] == "git_change"}
        for commit in found:
            if commit["sha"] in listed:
                listed[commit["sha"]]["pickaxe"] = name
                continue
            commits[(commit["repo_id"], commit["sha"])] = commit
            result["results"].append({"category": "git_change", "basis": "symbol_name_pickaxe", "pickaxe": name,
                                      "repo": current_repo, "ref": commit["sha"], "subject": commit["subject"],
                                      "committed_at": commit["committed_at"],
                                      "changes": [c for c in changes if c["sha"] == commit["sha"]],
                                      "next": {"argv": command_line(["git", "-C", current_repo, "show", commit["sha"],
                                                                     "-S", name])}})
    result["coverage"]["git"] = git_coverage
    result["scope"]["aliases"] = [{"absolute": k, **v} for k, v in aliases.items()]
    if args.no_sessions:
        result["coverage"]["sessions"] = {"state": "not_searched"}
        return rank(result)
    try:
        recordings, coverage = scan(args, store.root)
    except (ValueError, OSError) as error:
        result["coverage"]["sessions"] = {"state": "unavailable", "reason": str(error)}
        return rank(result)
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
        requests, humans = {}, []
        for event in recording["events"]:
            if event["kind"] == "user_message":
                requests.setdefault(event.get("turn"), event)
                if human(event):
                    humans.append(event)
        orders = [e["order"] for e in humans]
        calls = {}
        mention_paths = []
        recorded_root = recording.get("root")
        if recorded_root:
            recorded_root = normalized_root(remap(recorded_root, repository.root_map)[0])
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
            hunks_here = hunks_by_event.get((event["recording_id"], event["event_id"]), [])
            paired = calls.get(refkey(event["call_ref"])) if event["kind"] == "tool_result" and event.get("call_ref") else None
            if paired:
                # One row per exchange: the call row keeps the result's symbols, hunks and stronger basis.
                paired["symbols"] += match
                paired["hunks"] += hunks_here
                if BASIS_RANK.get(basis, 9) < BASIS_RANK.get(paired["basis"], 9):
                    paired["basis"] = basis
                continue
            row = clean(event, SimpleNamespace(preview_chars=300))
            entry = {"category": "recorded_activity" if direct else "text_mention", "basis": basis,
                     "evidence": row, "intent": intent(event, requests, humans, orders), "symbols": match,
                     "hunks": hunks_here, "ref": event["ref"], "next": next_for(event)}
            if event["kind"] == "tool_call":
                calls[refkey(event["ref"])] = entry
            result["results"].append(entry)
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
    return rank(result)


SESSIONS = Path(__file__).resolve().parents[1] / "sessions.py"
# Default table columns for compact rows; --fields overrides.
COMPACT_FIELDS = ["date", "source", "id", "text", "signals", "activity", "ref"]


def locator(ref):
    """A cite token when the recorded line is known; otherwise the JSON ref."""
    return cite.token(ref) if ref.get("line") is not None else ref


def compact(rows):
    """One row per recorded turn or commit, in rank order; --verbose keeps event rows and their evidence."""
    out, turns, commits = [], {}, {}
    for row in rows:
        if row["category"] == "git_change":
            item = {"source": "git", "date": when(row.get("committed_at"))[:10], "id": row["ref"],
                    "text": row["subject"][:160], "activity": f"{len(row.get('changes') or [])} files"}
            if row.get("pickaxe"):
                item["signals"] = ["pickaxe:" + row["pickaxe"]]
            commits[row["ref"]] = item
            out.append(item)
            continue
        if row["category"] == "commit_witness":
            sha = row["evidence"].get("sha")
            if sha in commits:
                commits[sha].setdefault("recorded_in", cite.recording_id(row["ref"]["file"]))
            else:
                out.append({"source": "witness", "id": sha, "text": "commit recorded in a session",
                            "ref": locator(row["ref"])})
            continue
        evidence, found = row["evidence"], row.get("intent", {})
        key = (evidence.get("session"), evidence.get("turn") or refkey(row["ref"]))
        item = turns.get(key)
        if item is None:
            request = found.get("request")
            item = {"source": "session", "date": when((request or {}).get("timestamp") or evidence.get("timestamp"))[:10],
                    "id": evidence.get("session"), "harness": evidence.get("harness"), "signals": [],
                    "counts": {}, "ref": locator(request["ref"] if request else row["ref"])}
            if request:
                item["text"] = request["text"][:160]
                if request["origin"] != "human":
                    item["signals"].append("delegated_request")
            turns[key] = item
            out.append(item)
        operation = (evidence.get("operation") or evidence.get("kind")) if row["category"] == "recorded_activity" else "mention"
        item["counts"][operation] = item["counts"].get(operation, 0) + 1
        item["signals"] += [s for s in found.get("signals", []) if s not in item["signals"]]
        if "possible_correction" in found.get("signals", []) and "followup" not in item:
            item["followup"] = found["followup"]["text"][:120]
    for item in turns.values():
        counts = item.pop("counts")
        item["mention_only"] = set(counts) == {"mention"}
        item["activity"] = ", ".join(f"{n} {op}" for op, n in sorted(counts.items(), key=lambda c: -c[1]))
        if not item["signals"]:
            del item["signals"]
    # A bare acknowledgement or slash-command echo that only mentions the target locates nothing; --verbose keeps it.
    return [item for item in out if not (item.pop("mention_only", False) and trivial(item.get("text", "")))]


def trivial(text):
    text = text.strip()
    return len(text) < 12 or text.startswith("<command-")


def summary(coverage):
    """Lane states and what limited them; --verbose returns every counter."""
    sessions = coverage.get("sessions", {})
    git = []
    for lane in coverage.get("git", []):
        entry = {"state": lane.get("state"), "path": lane.get("path")} if not lane.get("pickaxe") else \
            {"state": lane.get("state"), "pickaxe": lane["pickaxe"]}
        if lane.get("reason") or lane.get("error"):
            entry["reason"] = lane.get("reason") or lane.get("error")
        if lane.get("complete") is False and not entry.get("reason"):
            entry["reason"] = f"commit limit {lane.get('commits_limit')}"
        git.append(entry)
    out = {"git": git, "sessions": sessions.get("state")}
    for key in ("reason", "independent_sessions", "current_session_exclusion"):
        if key in sessions:
            out["sessions_" + key if key == "reason" else key] = sessions[key]
    # Name the limit that left each harness incomplete, so `partial` says what to raise.
    incomplete = {}
    for g in sessions.get("groups", []):
        if g.get("scan_complete") and g.get("text_complete"):
            continue
        reasons = [f"{g['files_omitted_limit']} older recordings over --max-recordings" if g.get("files_omitted_limit") else "",
                   "--max-scan-bytes" if g.get("byte_limit_reached") else "",
                   "--max-events" if g.get("event_limit_reached") else "",
                   "text budget" if not g.get("text_complete") else ""]
        incomplete[g["harness"]] = "; ".join(r for r in reasons if r) or "discovery limit; raise --max-candidates"
    if incomplete:
        out["scan_incomplete"] = incomplete
    if sessions.get("discovery_complete") is False:
        out["discovery_incomplete"] = True
    out["detail"] = "--verbose: every counter, alias, event row, hunk and expansion argv"
    return out


def deliver(document, args):
    """Bound output without persisting transcript text in graph reports."""
    from .graph_store import canonical
    from .graph_output import check_fields, project_rows, render_table
    verbose = getattr(args, "verbose", False)
    coverage = document.get("coverage", {})
    partial = any(c.get("state") in ("partial", "unavailable") for c in coverage.get("git", []))
    partial |= coverage.get("sessions", {}).get("state") in ("partial", "unavailable")
    rows = document["results"] if verbose else compact(document["results"])
    if not verbose:
        document["coverage"] = summary(coverage)
        scope = document.get("scope", {})
        if "aliases" in scope:
            scope["aliases"] = len(scope["aliases"])
        document["expand"] = {"session": command_line([sys.executable, str(SESSIONS), "context", "--ref", "REF",
                                                       "--mode", "exchange"]),
                              "git": command_line(["git", "-C", document["scope"]["repo"], "show", "ID"])}
    if getattr(args, "fields", None):
        # sessions.py calls the same column timestamp.
        args.fields = ["date" if f == "timestamp" else f for f in args.fields]
    check_fields(getattr(args, "fields", None), rows)
    fields = getattr(args, "fields", None) or (COMPACT_FIELDS if not verbose and getattr(args, "format", None) == "table" else None)
    total = len(rows)
    document["results"] = project_rows(rows[args.offset:args.offset + args.limit], getattr(args, "fields", None))
    def finish():
        following = args.offset + len(document["results"])
        document["delivery"] = {"total": total, "returned": len(document["results"]), "offset": args.offset,
                                "complete": following >= total}
        document["next"] = {"action": "history", "target": args.target, "offset": following,
                            "hint": "Repeat the same options; history is live. Increase --max-bytes if no row fits."} if following < total else None
        if following < total:
            document["results_truncated"] = f"{len(document['results'])} of {total} rows shown; --offset {following} or raise --limit / --max-bytes"
        # Paging truncates; only a git or session coverage gap makes the history incomplete.
        if partial:
            document["status"] = "partial"
    finish()
    while document["results"] and len(canonical(document)) + 1 > args.max_bytes:
        document["results"].pop()
        finish()
    if len(canonical(document)) + 1 > args.max_bytes:
        document = {"operation": "history", "status": "partial", "results": [], "delivery": {"complete": False},
                    "next": {"action": "history", "target": args.target, "hint": "Increase --max-bytes; metadata exceeds budget."}}
    print(render_table(document, fields) if getattr(args, "format", None) == "table" else canonical(document).decode())
