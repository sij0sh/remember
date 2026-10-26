"""Recording discovery: settings, candidate files, harness grouping, project filter."""
import json
import ntpath
import os
import posixpath
from pathlib import Path

from .profiles import PROFILES, detected, recorded_root
from .records import new_scope, records

# Default archive locations per harness; settings.json at the skill root overrides them.
SETTINGS = Path(os.environ.get("TRACE_SESSIONS_SETTINGS")
                or Path(__file__).resolve().parents[2] / "settings.json")
FALLBACK_RECORDINGS = {"pi": ["~/.pi/agent/sessions"], "codex": ["~/.codex/sessions"],
                       "claude-code": ["~/.claude/projects"]}


def header(path, max_record, max_scan, harness=None):
    """Bounded look at a recording's first records: (harness, recorded root, bytes read)."""
    scope = new_scope()
    root = None
    for _, value, _ in records(path, scope, min(max_record, max_scan), max_scan):
        if value:
            harness = harness or detected(value)
            root = root or (recorded_root(value, harness) if harness else None)
            if harness and root:
                break
    return harness, root, scope["bytes_scanned"]


def default_recordings():
    """Harness -> archive paths, in settings order; user/env references are expanded."""
    configured = dict(FALLBACK_RECORDINGS)
    if SETTINGS.is_file():
        data = json.loads(SETTINGS.read_text())
        recordings = data.get("recordings", {}) if isinstance(data, dict) else None
        if not isinstance(recordings, dict):
            raise ValueError(f"{SETTINGS}: 'recordings' must map harness names to paths")
        for harness, paths in recordings.items():
            paths = [paths] if isinstance(paths, str) else paths
            if harness not in PROFILES or not isinstance(paths, list) or not all(isinstance(x, str) for x in paths):
                raise ValueError(f"{SETTINGS}: recordings.{harness} must be a known harness "
                                 f"({', '.join(PROFILES)}) with a path or list of paths")
            configured[harness] = paths
        configured = {**{h: configured[h] for h in recordings}, **configured}
    return {h: [Path(os.path.expandvars(x)).expanduser() for x in paths] for h, paths in configured.items()}


def normalized_root(s):
    return ntpath.normcase(ntpath.normpath(s)) if "\\" in s or (len(s) > 1 and s[1] == ":") else posixpath.normpath(s)


def sources(options, scope):
    """[(harness label or None, path)]: explicit paths, else every configured harness."""
    if options.files:
        return [(None if options.harness == "auto" else options.harness, Path(f).expanduser()) for f in options.files]
    configured = default_recordings()
    if options.harness != "auto":
        configured = {options.harness: configured.get(options.harness, [])}
    scope["sources"] = [{"harness": h, "path": str(x), "exists": x.is_dir()} for h, paths in configured.items() for x in paths]
    found = [(h, x) for h, paths in configured.items() for x in paths if x.is_dir()]
    if not found:
        raise ValueError("No recording paths given and no default sessions directory exists: "
                         + ", ".join(str(x) for paths in configured.values() for x in paths)
                         + f"; edit {SETTINGS} or pass paths")
    return found


def candidate_files(options, scope, roots=None):
    """Group candidate recordings by harness, then select the newest of each harness.

    ``roots`` (a dict) collects (harness, recorded root or None) -> [recordings, newest
    mtime_ns] from the same bounded header reads, before the project filter applies.
    """
    candidates = {}
    seen = set()
    def add(label, path):
        if path.is_symlink() or path.suffix != ".jsonl":
            return
        stat = path.stat()
        key = stat.st_dev, stat.st_ino
        if key in seen:
            scope["duplicates_skipped"] += 1
            return
        seen.add(key)
        candidates.setdefault(label, []).append((stat.st_mtime_ns, str(path.absolute())))
    for label, path in sources(options, scope):
        if path.is_symlink():
            continue
        if path.is_file():
            add(label, path)
        elif path.is_dir():
            for base, dirs, files in os.walk(path, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not Path(base, d).is_symlink() and d not in (".git", "node_modules", ".venv"))
                for name in sorted(files):
                    add(label, Path(base, name))
        else:
            raise ValueError(f"Missing file/directory: {path}")
    scope["candidates_found"] = sum(map(len, candidates.values()))
    scope["files_omitted_candidates"] = 0
    scope["header_bytes_scanned"] = 0
    # Configured harnesses always get a group, so an empty archive is visible rather than silent.
    selected = {s["harness"]: [] for s in scope.get("sources", [])}
    zero = {"files_excluded_project": 0, "project_unknown": 0, "files_unrecognized": 0}
    group_scopes = {h: dict(zero) for h in selected}
    unrooted = {}
    wanted = {normalized_root(r) for r in (options.project, *options.also_roots) if r}
    for label, found in candidates.items():
        # Metadata only: order before applying the cap so recent recordings cannot be hidden.
        found.sort(reverse=True)
        scope["files_omitted_candidates"] += max(0, len(found) - options.max_candidates)
        if len(found) > options.max_candidates:
            # The --max-candidates value that would read every candidate of the largest group.
            scope["candidates_needed"] = max(scope.get("candidates_needed", 0), len(found))
        for mtime, name in found[:options.max_candidates]:
            # Header inspection is deliberately bounded and reported separately.
            forced = None if options.harness == "auto" else options.harness
            harness, root, read = header(name, options.max_record_bytes, 262144, forced)
            scope["header_bytes_scanned"] += read
            recognized = harness is not None
            harness = harness or label or "unknown"
            if roots is not None and recognized:
                seen_root = roots.setdefault((harness, root and normalized_root(root)), [0, mtime])
                seen_root[0] += 1
            selected.setdefault(harness, [])
            counts = group_scopes.setdefault(harness, dict(zero))
            if options.project:
                if not recognized:
                    # Not a recording in any known format (such as a tool's own JSONL in an archive).
                    counts["files_unrecognized"] += 1
                    continue
                if not root:
                    counts["project_unknown"] += 1
                    unrooted.setdefault(harness, []).append(name)
                    continue
                if normalized_root(root) not in wanted:
                    counts["files_excluded_project"] += 1
                    continue
            selected[harness].append(name)
    scope["discovery_complete"] = scope["files_omitted_candidates"] == 0
    scope["selection_order"] = "file_mtime_desc per harness; not semantic chronology"
    scope["project_filter"] = options.project or "all"
    if options.project and options.also_roots:
        scope["project_also"] = list(options.also_roots)
    order = list(PROFILES) + ["unknown"]
    groups = []
    for harness in sorted(selected, key=lambda h: order.index(h) if h in order else len(order)):
        files = selected[harness]
        gscope = {**new_scope(), **group_scopes[harness], "files_selected": min(len(files), options.max_files),
                  "files_omitted_limit": max(0, len(files) - options.max_files)}
        if unrooted.get(harness):
            gscope["project_unknown_files"] = unrooted[harness][:3]
        # A rootless recording is reported by project_unknown, not as a scan limit.
        if not scope["discovery_complete"] or gscope["files_omitted_limit"]:
            gscope["scan_complete"] = False
        groups.append((harness, files[:options.max_files], gscope))
    return groups
