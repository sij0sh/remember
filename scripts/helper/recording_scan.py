"""Shared bounded recording scan and lineage mechanics; no retained transcript data."""
import os
from pathlib import Path
from types import SimpleNamespace
from .graph_store import canonical_path
from .reader import scan_file
from .budget import check
from .options import TEXT_BYTES_DEFAULT, TEXT_BYTES_AUTO_CAP
from . import zstd

def _size(path):
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def text_budget(args, files, gscope):
    """Explicit --max-text-bytes, else the bytes this group will scan, within the default and the cap."""
    if args.max_text_bytes is not None:
        gscope["text_budget"] = args.max_text_bytes
        return args.max_text_bytes
    size = sum(_size(path) for path in files)
    budget = max(TEXT_BYTES_DEFAULT, min(size, args.max_scan_bytes, TEXT_BYTES_AUTO_CAP))
    gscope["text_budget"] = budget
    return budget


def _snapshot(path, scope, before):
    try:
        stat = Path(path).stat()
        scanned = scope.get("bytes_scanned", 0) - before
        # A compressed artifact's decompressed bytes say nothing about the source size;
        # its reader reports stream EOF instead.
        complete = bool(scope.get("source_eof")) if zstd.compressed(path) else scanned >= stat.st_size
        scope.setdefault("snapshot", []).append({
            "file": canonical_path(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "inode": stat.st_ino, "bytes_scanned": scanned, "scan_complete": complete})
        return complete
    except OSError:
        return None


def lineages(recordings):
    """Each recording's independent session: a subagent or fork counts with the session it was recorded from."""
    by_file = {r["file"]: r for r in recordings}
    def key(r, depth=0):
        parent = r.get("parent_session")
        if not parent:
            return r["session"]
        # Pi records the parent as a file path; resolve it to that recording's own lineage.
        found = by_file.get(parent)
        return key(found, depth + 1) if found is not None and found is not r and depth < 32 else parent
    return {r["file"]: key(r) for r in recordings}


def scan_group(files, options, counters):
    counters["bytes_selected"] = sum(_size(path) for path in files)
    recordings = []
    for path in files:
        check()
        if counters.get("event_limit_reached") or counters["bytes_scanned"] >= options.max_scan_bytes:
            counters["scan_complete"] = False
            counters["scan_limit_reached"] = counters["bytes_scanned"] >= options.max_scan_bytes
            counters["byte_limit_reached"] = counters["scan_limit_reached"]
            break
        before = counters["bytes_scanned"]
        recording = scan_file(path, options, counters)
        recordings.append(recording)
        counters["files_scanned"] += 1
        recording["scan_complete"] = _snapshot(path, counters, before) is not False
    return recordings


class DiscoveryUnavailable(ValueError):
    """Recording discovery failed before any scan (e.g. no configured directories)."""


# Harness-exported current-session ids. Claude Code exports CLAUDE_CODE_SESSION_ID.
SESSION_ENV = ("CODEX_THREAD_ID", "PI_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
               "DSH_SESSION_ID")


def env_sessions():
    return {v for v in (os.environ.get(name) for name in SESSION_ENV) if v}


def current_sessions(args):
    """Sessions to drop: --exclude-session plus the harness's own current-session id."""
    excluded = set(getattr(args, "exclude_session", []))
    excluded.update(env_sessions())
    return excluded


def scan_project(args, root, scope):
    """Discover and scan one project's recordings, dropping excluded sessions.

    Returns (kept, groups, keys, excluded, scanned): groups are per-harness counters, keys map file to
    independent session, scanned counts recordings before exclusion. Discovery failure raises
    DiscoveryUnavailable; scan and deadline errors propagate unchanged."""
    from .discovery import candidate_files
    from .options import DiscoveryOptions, ScanOptions
    options = DiscoveryOptions(files=tuple(getattr(args, "recordings", [])), project=root,
                               also_roots=tuple(getattr(args, "project_root", [])),
                               max_files=args.max_recordings, max_candidates=args.max_candidates)
    # Store full text for the bytes each group will scan, so a target mention is not cut off.
    policy = SimpleNamespace(max_text_bytes=None, max_scan_bytes=args.max_scan_bytes)
    try:
        candidates = candidate_files(options, scope)
    except ValueError as error:
        raise DiscoveryUnavailable(str(error)) from None
    recordings, groups = [], []
    for harness, files, counters in candidates:
        check()
        scan = ScanOptions(harness=harness, max_scan_bytes=args.max_scan_bytes, max_events=args.max_events,
                           max_text_bytes=text_budget(policy, files, counters))
        recordings.extend(scan_group(files, scan, counters))
        groups.append({"harness": harness, **counters})
    keys = lineages(recordings)
    excluded = current_sessions(args)
    kept = []
    for recording in recordings:
        if {recording["session"], recording["file"], keys[recording["file"]]} & excluded:
            continue
        for event in recording["events"]:
            event["independent_session"] = keys[recording["file"]]
        kept.append(recording)
    return kept, groups, keys, excluded, len(recordings)


def schedule(groups, files, exclude=(), hits=None):
    """Invocation-wide read order over harness groups (each newest first).

    First half (match lane): probe-hit files by hit count, then newest-first, harnesses rotated.
    Second half: the rest alternating oldest/newest, harnesses rotated.
    Returns [(harness, path, lane)] and omitted count.
    """
    exclude = {canonical_path(p) for p in exclude}
    hits = hits or {}
    queues = [(h, [f for f in fs if canonical_path(f) not in exclude]) for h, fs, _ in groups]
    # Stable sort keeps newest-first order among equal hit counts and among non-hits.
    queues = [(h, sorted(fs, key=lambda f: -hits.get(f, 0))) for h, fs in queues]
    queues = [(h, list(fs)) for h, fs in queues if fs]
    total = sum(len(fs) for _, fs in queues)
    newest_quota = (min(files, total) + 1) // 2
    order = []

    def rotate(take, limit):
        while queues and len(order) < limit:
            for harness, pending in queues:
                if pending and len(order) < limit:
                    order.append((harness, *take(harness, pending)))
            queues[:] = [(h, fs) for h, fs in queues if fs]

    def newest(harness, pending):
        return pending.pop(0), "match"
    oldest_next = {}

    def context(harness, pending):
        # Per harness: oldest remaining first, then newest remaining, alternating.
        old = oldest_next.get(harness, True)
        oldest_next[harness] = not old
        return (pending.pop() if old else pending.pop(0)), "context"
    rotate(newest, newest_quota)
    rotate(context, files)
    return order, total - len(order)


def _probe(candidates, exclude_files, needles, budget):
    """Probe hits when seeds exist and candidates exceed the byte budget (D130); else (None, None)."""
    from .evidence_candidates import probe
    exclude = {canonical_path(p) for p in exclude_files}
    files = []
    for _, paths, _ in candidates:
        for path in paths:
            if canonical_path(path) in exclude:
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            files.append((path, stat.st_size, stat.st_mtime_ns))
    if not needles or sum(f[1] for f in files) <= budget:
        return None, None
    return probe(files, needles, budget // 2)


def scan_scheduled(args, root, scope, exclude_files=(), needles=()):
    """One discovery, one shared file/byte/event allowance across harnesses (investigate, inspect).

    The context lane reserves half the byte allowance, or EXPLORE_SHARE after a seed probe whose
    bytes are charged to the same allowance; unused match bytes lend forward.
    Returns (kept, groups, keys, excluded, scanned, allocation)."""
    from .discovery import candidate_files
    from .options import DiscoveryOptions, EXPLORE_SHARE, ScanOptions
    options = DiscoveryOptions(files=tuple(getattr(args, "recordings", [])), project=root,
                               also_roots=tuple(getattr(args, "project_root", [])),
                               max_files=args.max_candidates, max_candidates=args.max_candidates)
    try:
        candidates = candidate_files(options, scope)
    except ValueError as error:
        raise DiscoveryUnavailable(str(error)) from None
    hits, probed = _probe(candidates, exclude_files, needles, args.max_scan_bytes)
    order, omitted = schedule(candidates, args.max_recordings, exclude_files, hits)
    counters = {h: c for h, _, c in candidates}
    for c in counters.values():
        c["files_selected"], c["files_omitted_limit"] = 0, 0
    policy = SimpleNamespace(max_text_bytes=None, max_scan_bytes=args.max_scan_bytes)
    text = {h: text_budget(policy, [p for hh, p, _ in order if hh == h], counters[h]) for h in counters}
    total_bytes, total_events = args.max_scan_bytes, args.max_events
    if probed:
        total_bytes = max(1, total_bytes - probed["bytes_probed"])
        match_bytes = total_bytes - int(total_bytes * EXPLORE_SHARE)
    else:
        match_bytes = total_bytes - total_bytes // 2
    used_bytes = used_events = 0
    recordings, reads, lanes = [], [], {"match": 0, "context": 0}
    stopped = None
    for harness, path, lane in order:
        check()
        cap = (match_bytes if lane == "match" else total_bytes) - used_bytes
        if cap <= 0 or used_events >= total_events:
            stopped = "byte_limit" if cap <= 0 else "event_limit"
            if lane == "match":
                continue  # the context reservation stays available
            break
        group = counters[harness]
        before_bytes, before_events = group["bytes_scanned"], group["events_projected"]
        scan = ScanOptions(harness=harness, max_scan_bytes=before_bytes + cap,
                           max_events=before_events + total_events - used_events, max_text_bytes=text[harness])
        recording = scan_file(path, scan, group)
        group["files_scanned"] += 1
        group["files_selected"] += 1
        recording["scan_complete"] = _snapshot(path, group, before_bytes) is not False
        used_bytes += group["bytes_scanned"] - before_bytes
        used_events += group["events_projected"] - before_events
        recording["schedule_lane"] = lane
        recordings.append(recording)
        lanes[lane] += 1
        try:
            reads.append(os.stat(path).st_mtime_ns)
        except OSError:
            pass
    unread = omitted + len(order) - len(recordings)
    groups = []
    for harness, _, c in candidates:
        if unread or stopped:
            c["scan_complete"] = False
        groups.append({"harness": harness, **c})
    keys = lineages(recordings)
    excluded = current_sessions(args)
    kept = []
    for recording in recordings:
        if {recording["session"], recording["file"], keys[recording["file"]]} & excluded:
            continue
        for event in recording["events"]:
            event["independent_session"] = keys[recording["file"]]
        kept.append(recording)
    policy = (f"seed-probe hits then newest-first match, {EXPLORE_SHARE:.0%} age-diverse context; "
              "probe bytes charged; totals across harnesses" if probed else
              "half newest-first match, half age-diverse context; totals across harnesses")
    allocation = {"policy": policy, **({"probe": probed} if probed else {}),
                  "files": {"allowed": args.max_recordings, "read": len(recordings), **lanes, "omitted": unread},
                  "bytes": {"allowed": args.max_scan_bytes, "match_reserved_max": match_bytes,
                            "read": used_bytes + (probed["bytes_probed"] if probed else 0)},
                  "events": {"allowed": total_events, "read": used_events}, "stopped": stopped,
                  "age_horizon_ns": min(reads) if reads else None}
    return kept, groups, keys, excluded, len(recordings), allocation
