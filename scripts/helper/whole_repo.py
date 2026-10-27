"""Whole-repository text for small captures, a directory summary for larger ones; printed, never written to disk."""
import collections
from pathlib import PurePosixPath

# A capture at or under both limits is cheaper to read than to query.
MAX_LINES = 3000
MAX_BYTES = 200_000
# Bare map prints whole text only up to an analyze page; `map --whole` raises the cap to MAX_BYTES.
TEXT_BYTES = 24_000
# Generated dependency pins: large, rarely informative, omitted and named.
LOCKFILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "Cargo.lock", "poetry.lock", "uv.lock",
             "Pipfile.lock", "Gemfile.lock", "composer.lock", "go.sum", "flake.lock"}


def line_count(text):
    return text.count("\n") + int(bool(text) and not text.endswith("\n"))


# Directory rows in the large-repository summary; the largest folders are expanded first.
SUMMARY_ROWS = 25
SUMMARY_FILES = 15


def readable(graph):
    files, skipped = [], []
    for row in graph.body["source"]["files"]:
        if row["path"].rsplit("/", 1)[-1] in LOCKFILES:
            skipped.append(f"{row['path']} (lockfile)")
        elif row["text"] is None:
            skipped.append(f"{row['path']} ({row['state']})")
        else:
            files.append(row)
    return files, skipped


def header(graph, warnings, skipped, primer=None):
    source = graph.body["source"]
    excluded = source["inventory"].get("exclusions", [])
    out = [f"root: {source['root']}", f"head: {source['git'].get('head') or 'none'}", f"snapshot: {graph.snapshot}"]
    if skipped:
        out.append("not shown: " + ", ".join(skipped[:20]) + (", ..." if len(skipped) > 20 else ""))
    if excluded:
        out.append(f"excluded by rules: {len(excluded)} paths (" + ", ".join(e["path"] for e in excluded[:10])
                   + (", ..." if len(excluded) > 10 else "") + ")")
    for w in warnings:
        if w["kind"] == "uncaptured_reference":
            refs = ", ".join(f"{r['path']}:{r['line']}" for r in w["examples"])
            slash = "" if w.get("source") == "manifest" else "/"
            out.append(f"not captured but used: {w['path']}{slash} (referenced at {refs})")
        elif w["kind"] == "missing_reference":
            refs = ", ".join(f"{r['path']}:{r['line']}" for r in w["examples"])
            out.append(f"referenced but missing: {w['path']} (named at {refs})")
        elif w["kind"] == "dominant_file" and w.get("generated"):
            out.append(f"dominant file: {w['path']} holds {w['share']:.0%} of nodes; generated ({w['generated']}), "
                       "left out of analyze rankings")
        elif w["kind"] == "dominant_file":
            out.append(f"dominant file: {w['path']} holds {w['share']:.0%} of nodes; rankings skew toward it")
        elif w["kind"] == "generated_files":
            out.append(f"generated/vendored, left out of analyze rankings: {w['count']} files ("
                       + ", ".join(w["examples"]) + (", ..." if w["count"] > len(w["examples"]) else "") + ")")
    if primer:
        out += ["", *primer]
    return out


def render(graph, warnings, max_lines=MAX_LINES, primer=None, max_bytes=TEXT_BYTES):
    """The full source as plain text, or None when the capture exceeds the limits."""
    files, skipped = readable(graph)
    lines = sum(line_count(r["text"]) for r in files)
    size = sum(len(r["text"].encode()) for r in files)
    if not files or not max_lines or lines > max_lines or size > min(max_bytes, MAX_BYTES):
        return None
    out = [f"WHOLE REPOSITORY: all {len(files)} readable captured files ({lines} lines) follow.",
           "Read them directly; graph queries add little at this size. `map --kind file` returns the JSON listing.",
           *header(graph, warnings, skipped, primer)]
    for row in files:
        out += ["", f"==> {row['path']} <==", row["text"].removesuffix("\n")]
    text = "\n".join(out) + "\n"
    return text if len(text.encode()) <= max_bytes else None


def summary(graph, warnings, max_lines=MAX_LINES, primer=None):
    """Directory totals, largest files and coverage for a capture too large to print whole."""
    files, skipped = readable(graph)
    sizes = {r["path"]: line_count(r["text"]) for r in files}
    totals = collections.defaultdict(lambda: [0, 0, None])  # files, lines, largest path
    for path, count in sizes.items():
        for parent in [*PurePosixPath(path).parents][:-1]:
            row = totals[parent.as_posix()]
            row[0] += 1
            row[1] += count
            if row[2] is None or count > sizes[row[2]]:
                row[2] = path
    children = collections.defaultdict(list)
    for folder in totals:
        children[PurePosixPath(folder).parent.as_posix()].append(folder)
    # Start at top-level folders; split the largest folder until the table is full.
    shown, loose = set(children["."]), [p for p in sizes if "/" not in p]
    while True:
        splittable = sorted((f for f in shown if children[f]), key=lambda f: -totals[f][1])
        if not splittable:
            break
        folder = splittable[0]
        direct = [p for p in sizes if PurePosixPath(p).parent.as_posix() == folder]
        if len(shown) - 1 + len(children[folder]) + bool(direct) > SUMMARY_ROWS:
            break
        shown.discard(folder)
        shown.update(children[folder])
        if direct:
            totals[folder + "/*"] = [len(direct), sum(sizes[p] for p in direct), max(direct, key=sizes.get)]
            shown.add(folder + "/*")
    if loose:
        totals["./*"] = [len(loose), sum(sizes[p] for p in loose), max(loose, key=sizes.get)]
        shown.add("./*")
    if len(shown) > SUMMARY_ROWS:
        ordered = sorted(shown, key=lambda f: (-totals[f][1], f))
        remainder = ordered[SUMMARY_ROWS - 1:]
        totals["(other directories)"] = [sum(totals[f][0] for f in remainder),
                                         sum(totals[f][1] for f in remainder),
                                         max((totals[f][2] for f in remainder), key=sizes.get)]
        shown = set(ordered[:SUMMARY_ROWS - 1]) | {"(other directories)"}
    coverage = graph.body["coverage"]
    gaps = coverage.get("gaps", {})
    size = sum(len(r["text"].encode()) for r in files)
    whole = sum(sizes.values()) <= max_lines and size <= MAX_BYTES
    out = [f"REPOSITORY SUMMARY: {len(files)} readable files, {sum(sizes.values())} lines, {size} bytes "
           f"(whole text: {TEXT_BYTES} bytes; `map --whole`: {max_lines} lines / {MAX_BYTES} bytes).",
           *([f"`map --whole` prints all {len(files)} files ({size} bytes)."] if whole else []),
           *header(graph, warnings, skipped, primer),
           f"graph: {coverage.get('nodes', 0)} nodes, {coverage.get('edges', 0)} edges, gaps "
           + (", ".join(f"{k} {v}" for k, v in sorted(gaps.items())) or "none"),
           "", "DIRECTORIES (files, lines, largest file; `/*` = files directly in that folder)"]
    width = max((len(f) for f in shown), default=0)
    for folder in sorted(shown):
        count, total, big = totals[folder]
        out.append(f"  {folder.ljust(width)}  {count:>5}  {total:>7}  {big} ({sizes[big]})")
    out += ["", "LARGEST FILES (lines)"]
    out += [f"  {sizes[p]:>7}  {p}" for p in sorted(sizes, key=lambda p: (-sizes[p], p))[:SUMMARY_FILES]]
    out += ["", "NEXT: `map DIR --kind file`; `analyze largest|fanin|orphans|cycles|untested`; "
            "`find TEXT --path GLOB`; `status` for coverage."]
    return "\n".join(out) + "\n"
