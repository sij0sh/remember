"""Invocation-local provider registry; shared source, Git and recording datasets."""
import os
from types import SimpleNamespace
from . import (catalog, cite, commit_links, edit_hunks, episode_links, file_observations, message_segments, git_file_history, graph_cli, graph_queries,
                operation_facts, repo_files, summaries)
from .engine import Input
from .root_map import parse_root_map, remap
from .git_resolve import GitResolver
from .graph_store import canonical_path, digest
from .options import DiscoveryOptions, ScanOptions, TEXT_BYTES_DEFAULT
from .projection import clean, human
from .recording_scan import DiscoveryUnavailable, lineages, scan_group, scan_project, scan_scheduled, text_budget
from .discovery import candidate_files
from .records import RecordCache


def session_row(recording, rows):
    """One recording summarized from its event rows: span, activity and the request that opened it."""
    stamps = sorted(r["timestamp"] for r in rows if r.get("timestamp"))
    request = next((r for r in rows if r.get("kind") == "user_message" and human(r) and message_segments.prose(r)), None)
    return {"session": recording["session"], "parent_session": recording.get("parent_session"),
            "independent_session": next((r.get("independent_session") for r in rows), None),
            "harness": recording.get("harness"), "harness_version": recording.get("harness_version"),
            "recording_id": next((r.get("recording_id") for r in rows), None) or recording["file"],
            "root": recording.get("root"), "started_at": stamps[0] if stamps else None,
            "ended_at": stamps[-1] if stamps else None, "events": len(rows),
            "user_messages": sum(r.get("kind") == "user_message" for r in rows),
            "tool_calls": sum(r.get("kind") == "tool_call" for r in rows),
            "failed_calls": sum(r.get("kind") == "tool_call" and r.get("outcome") == "failed" for r in rows),
            "repo_paths": len({r["repo_path"] for r in rows if r.get("repo_path")}),
            "first_request": message_segments.prose(request)[:500] if request else None,
            "first_request_cite": request.get("cite") if request else None}


class Providers:
    def __init__(self, args, store, deadline):
        self.args, self.store, self.deadline = args, store, deadline
        self.loaded, self.tables, self.coverage = [], {}, {}
        self.graph = None
        self.recordings = []
        self.resolver = GitResolver()
        self.cache = RecordCache(2 * 1024 * 1024)
        self.root_map = parse_root_map(getattr(args, "root_map", []))
        self.worktree = store.root
        self.repo_id = None
        self.roots = {}
        self.mapped_events = set()
        # Focused scans read one referenced recording first instead of the bounded archive.
        self.recording_files = None
        # Scheduled scans (investigate, inspect): invocation-wide totals, active file excluded.
        self.scheduled, self.exclude_files, self.allocation = False, (), None
        self.episodes = None  # episode lookup {by_id, by_request} once the episodes provider loads
        # Exact seed needles (paths, errors, SHAs) for the scheduled scan's probe.
        self.needles = ()

    def load(self, names):
        for provider in catalog.providers_for(names):
            if provider not in self.loaded:
                self.deadline.check()
                getattr(self, "load_" + provider)()
                self.loaded.append(provider)
                self.deadline.check()

    def input(self, name):
        definition = catalog.CATALOG[name]
        return Input(name, self.tables[name], self.coverage[definition.provider],
                     list(definition.columns), definition.columns, definition.grain)

    def load_graph(self):
        self.graph = graph_cli.get_graph(self.store, self.args)
        for item in graph_queries.inputs(self.graph):
            self.tables[item.name] = item.rows
        self.tables["files"] = [{**row, "content": row.get("text"),
                                  "lines": len(row["text"].splitlines()) if row.get("text") is not None else None,
                                  "language": (self.graph.files.get(row["path"]) or {}).get("language")}
                                 for row in self.tables["files"]]
        self.by_path = {row["path"]: row for row in self.tables["files"]}
        self.items_by_path = {}
        for node in self.graph.nodes.values():
            if node["kind"] != "symbol":
                continue
            item = {**self.graph.public(node), "kind": node.get("symbol_kind"), "repo_id": self.store.root}
            self.items_by_path.setdefault(item["path"], []).append(item)
        doc = self.graph.document("query", [])
        gaps = graph_cli.incomplete(doc, self.graph)
        self.coverage["graph"] = {"complete": not gaps, "incomplete_reasons": sorted({g["area"] for g in gaps}),
                                  "snapshot": self.graph.snapshot, "gaps": len(self.graph.body["gaps"])}

    def candidate_recordings(self):
        """Bounded discovery of every project-matched candidate, without scanning them."""
        options = DiscoveryOptions(
            files=tuple(getattr(self.args, "recordings", None) or ()), harness="auto",
            project=self.store.root, max_files=getattr(self.args, "max_candidates", 20000),
            max_candidates=getattr(self.args, "max_candidates", 20000),
            max_record_bytes=getattr(self.args, "max_record_bytes", 2 * 1024 * 1024),
            also_roots=tuple(m.partition("=")[0] for m in getattr(self.args, "root_map", None) or ()))
        return candidate_files(options, {"duplicates_skipped": 0})

    def match_recordings(self, sha, subject):
        """Find candidate recordings whose bytes name this commit, before a bounded rescan."""
        needles = [sha.lower().encode()]
        if subject and len(subject.strip()) >= 8:
            needles.append(subject.strip()[:120].lower().encode())
        budget = getattr(self.args, "max_scan_bytes", 64 * 1024 * 1024)
        per_file = getattr(self.args, "max_record_bytes", 2 * 1024 * 1024)
        files, read, matched, scanned = 0, 0, [], 0
        for _, group_files, _ in self.candidate_recordings():
            files += len(group_files)
            for name in group_files:
                if read >= budget:
                    break
                try:
                    with open(name, "rb") as stream:
                        blob = stream.read(min(per_file, budget - read))
                except OSError:
                    continue
                read += len(blob)
                scanned += 1
                if any(needle in blob.lower() for needle in needles):
                    matched.append(name)
            if read >= budget:
                break
        return matched, {"candidates": files, "files_read": scanned,
                         "files_matched": len(matched), "bytes_scanned": read,
                         "complete": read < budget, "basis": "sha_or_subject_text"}

    def reload_recordings(self, files):
        """Replace recording-derived tables with a targeted scan, keeping Git facts loaded."""
        self.recording_files = list(files)
        self.recordings = []
        keep = {"recordings", "operations", "checkpoints", "links", "episodes", "segments",
                "observations"}
        self.loaded = [name for name in self.loaded if name not in keep]
        self.episodes = None
        for key in ("events", "rows", "sessions", "operations", "checkpoints", "summaries", "summary_items",
                    "summary_refs", "commit_links", "episodes", "episode_links", "message_segments",
                    "file_observations", "change_observations"):
            self.tables.pop(key, None)
        self.load({"recordings", "operations", "checkpoints", "commit_links"})

    def load_recordings(self):
        scope = {"duplicates_skipped": 0}
        focused = bool(self.recording_files)
        try:
            if focused:
                # An exact ref is already the anchor: read that file, still within the same budgets.
                files = tuple(str(item) for item in self.recording_files)
                groups, excluded = [], ()
                options = DiscoveryOptions(files=files, harness="auto", project=None, max_files=len(files),
                                           max_candidates=max(len(files), 1),
                                           max_record_bytes=getattr(self.args, "max_record_bytes", 2 * 1024 * 1024))
                for harness, group_files, counters in candidate_files(options, scope):
                    policy = SimpleNamespace(max_text_bytes=None, max_scan_bytes=self.args.max_scan_bytes)
                    scan = ScanOptions(harness=harness, max_scan_bytes=self.args.max_scan_bytes,
                                       max_events=self.args.max_events,
                                       max_text_bytes=text_budget(policy, group_files, counters))
                    self.recordings.extend(scan_group(group_files, scan, counters))
                    groups.append({"harness": harness, **counters})
                keys = lineages(self.recordings)
                for recording in self.recordings:
                    for event in recording["events"]:
                        event["independent_session"] = keys[recording["file"]]
            elif self.scheduled:
                self.recordings, groups, _, excluded, self.scan_count, self.allocation = scan_scheduled(
                    self.args, self.store.root, scope, self.exclude_files, self.needles)
            else:
                self.recordings, groups, _, excluded, self.scan_count = scan_project(self.args, self.store.root, scope)
        except DiscoveryUnavailable as error:
            if getattr(self.args, "recordings", []):
                raise
            self.tables["events"] = self.tables["rows"] = self.tables["sessions"] = []
            self.coverage["recordings"] = {"complete": False, "state": "unavailable", "error": str(error)}
            return
        self.roots = {r["file"]: r.get("root") for r in self.recordings}
        self.scan_groups, self.scan_scope, self.scan_excluded = groups, scope, excluded
        rows, sessions = [], []
        for recording in self.recordings:
            first = len(rows)
            for event in recording["events"]:
                row = clean(event, complete=True)
                row = cite.tagged(row, event["ref"])
                row.update(repo_path=self.repo_path(event))
                rows.append(row)
            sessions.append(session_row(recording, rows[first:]))
        self.tables["events"] = self.tables["rows"] = rows
        self.tables["sessions"] = sessions
        reasons = []
        if not scope.get("discovery_complete", True): reasons.append("discovery_limit")
        if any(not g.get("scan_complete", True) for g in groups): reasons.append("scan_limit")
        if any(not g.get("text_complete", True) for g in groups): reasons.append("text_limit")
        if any(g.get("project_unknown") for g in groups): reasons.append("project_unknown")
        if any(g.get("files_compressed_unavailable") for g in groups): reasons.append("decoder_unavailable")
        if any(g.get("decompression_failed") for g in groups): reasons.append("decompression_failed")
        keys = ("records_scanned", "items_interpreted", "records_unrepresented", "interpretation_unsupported",
                "interpretation_gaps", "unknown_records", "ignored_items", "ancestry_unknown", "ancestry_cycles",
                "unpaired_results", "calls_without_result", "ambiguous_call_ids", "tail_alignment_skipped",
                "tail_alignment_bytes")
        interpretation = {key: sum(g.get(key, 0) for g in groups) for key in keys}
        interpretation["complete"] = not (interpretation["interpretation_unsupported"]
                                          or interpretation["interpretation_gaps"]
                                          or interpretation["ancestry_cycles"]
                                          or interpretation["ambiguous_call_ids"])
        self.coverage["recordings"] = {"complete": not reasons, "incomplete_reasons": reasons,
                                       "files_scanned": sum(g.get("files_scanned", 0) for g in groups),
                                       "project": self.store.root,
                                       "scope": (("selected recording" if len(files) == 1
                                                  else "selected recordings") if focused else "matched archive"),
                                       "current_session_exclusion": ("not_applied_focused" if focused else
                                                                     "configured" if excluded else "unknown"),
                                       "interpretation": interpretation,
                                       **({"allocation": self.allocation} if self.allocation else {}),
                                       "signature": digest([g.get("snapshot", []) for g in groups])}

    def session_coverage(self):
        """Loaded recordings' coverage in target-history form, so inspect reuses this scan."""
        state = self.coverage.get("recordings", {})
        if state.get("state") == "unavailable":
            return {"state": "unavailable", "reason": state.get("error")}
        groups, scope = self.scan_groups, self.scan_scope
        partial = not scope.get("discovery_complete", True) or any(
            not g.get("scan_complete", True) or not g.get("text_complete", True) for g in groups)
        return {**{k: v for k, v in scope.items() if k != "snapshot"}, "groups": groups,
                "state": "partial" if partial else "searched", "excluded_sessions": sorted(self.scan_excluded),
                "current_session_exclusion": "configured" if self.scan_excluded else "unknown; pass --exclude-session",
                "recordings_excluded": max(0, getattr(self, "scan_count", len(self.recordings)) - len(self.recordings)),
                "independent_sessions": len({e.get("independent_session") for r in self.recordings
                                             for e in r["events"][:1]}),
                **({"allocation": self.allocation} if self.allocation else {})}

    def load_git(self):
        root = self.resolver.repo(self.store.root)
        self.repo_id = self.resolver.identity(root)
        if root:
            commits, changes, coverage = git_file_history.history(self.resolver, root, self.args.max_commits, None)
        else:
            commits, changes, coverage = [], [], {"complete": False, "error": "not a Git repository"}
        self.tables["commits"] = [{**row, "ref": row["sha"]} for row in commits]
        self.tables["changes"] = changes
        self.coverage["git"] = coverage

    def load_links(self):
        rows = commit_links.witnesses(self.recordings, self.resolver, self.cache, self.resolver.repo(self.store.root))
        self.tables["commit_links"] = rows
        missing = sum(row.get("confidence") == "unresolved" and not row.get("failed") for row in rows)
        self.coverage["links"] = {"complete": not missing and self.coverage["recordings"]["complete"], "unresolved": missing}

    def load_operations(self):
        rows, coverage = operation_facts.operations(self.recordings, self.cache)
        self.tables["operations"] = rows
        coverage["source"] = "recorded events; no fresh scan"
        coverage["complete"] = coverage.get("complete", True) and self.coverage["recordings"]["complete"] \
            and self.coverage["recordings"].get("interpretation", {}).get("complete", True)
        self.coverage["operations"] = coverage

    def load_segments(self):
        rows, coverage = message_segments.rows(self.recordings)
        self.tables["message_segments"] = rows
        coverage["source"] = "stored message text; truncated messages are partial"
        coverage["complete"] = coverage["complete"] and self.coverage["recordings"]["complete"]
        self.coverage["segments"] = coverage

    def load_episodes(self):
        rows, links, coverage, self.episodes = episode_links.build(
            self.recordings, self.tables.get("operations", []), self.tables.get("commit_links", []), self.store.root)
        self.tables["episodes"], self.tables["episode_links"] = rows, links
        self.coverage["episodes"] = coverage

    def load_observations(self):
        rows, changes, coverage = file_observations.build(
            self.recordings, self.cache, self.tables.get("operations", []), self.repo_path)
        self.tables["file_observations"], self.tables["change_observations"] = rows, changes
        coverage["complete"] = coverage["complete"] and self.coverage["operations"]["complete"]
        self.coverage["observations"] = coverage

    def load_checkpoints(self):
        rows, coverage = operation_facts.checkpoints(self.recordings, self.cache)
        self.tables["checkpoints"] = rows
        coverage["source"] = "recorded checkpoints; boundaries resolved on recorded lanes"
        coverage["complete"] = coverage.get("complete", True) and self.coverage["recordings"]["complete"]
        self.coverage["checkpoints"] = coverage

    def load_summaries(self):
        rows, items, refs, coverage = summaries.build(self.recordings, self.cache,
                                                      self.tables.get("checkpoints", []))
        self.tables["summaries"], self.tables["summary_items"], self.tables["summary_refs"] = rows, items, refs
        coverage["complete"] = coverage.get("complete", True) and self.coverage["recordings"]["complete"]
        self.coverage["summaries"] = coverage

    def load_edits(self):
        self.repo_id = self.resolver.identity(self.resolver.repo(self.store.root)) or self.store.root
        hunks, symbols, coverage = edit_hunks.derive(self.recordings, self.cache, self)
        self.tables["edit_hunks"], self.tables["edit_symbols"] = hunks, symbols
        coverage["complete"] = coverage.get("complete", True) and all(self.coverage[p]["complete"] for p in ("recordings", "graph"))
        coverage["source_basis"] = "pinned_source"
        self.coverage["edits"] = coverage

    def load_status(self):
        root = self.resolver.repo(self.store.root)
        if root:
            rows, _, coverage = repo_files.snapshot(self.resolver, root, self.args.max_files, self.args.max_file_bytes)
        else:
            rows, coverage = [], {"complete": False, "error": "not a Git repository"}
        self.tables["file_status"], self.coverage["status"] = rows, coverage

    def repo_path(self, event):
        path, location = event.get("path"), event.get("path_location")
        if not path or location not in ("repository", "external"):
            return None
        if location == "repository":
            root = self.roots.get(event.get("recording_id"))
            if not root: return None
            path = os.path.join(root, path)
        path, _ = remap(path, self.root_map)
        path = os.path.abspath(path)
        try:
            real, root = canonical_path(path), canonical_path(self.store.root)
            if os.path.commonpath([real, root]) == root:
                return os.path.relpath(real, root).replace(os.sep, "/")
        except ValueError:
            pass
        return None

    def current_lines(self, path):
        row = self.by_path.get(path, {})
        return (edit_hunks.split_lines(row["content"]), None) if row.get("content") is not None else (None, "source_unavailable")

    def has_items(self, path):
        return (self.graph.files.get(path) or {}).get("parse_state") in ("parsed", "parsed_with_errors")

    def items_for(self, path):
        return self.items_by_path.get(path, [])
