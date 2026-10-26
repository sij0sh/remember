"""Whole-repository text for captures small enough to read directly; printed, never written to disk."""

# A capture at or under both limits is cheaper to read than to query.
MAX_LINES = 3000
MAX_BYTES = 200_000
# Generated dependency pins: large, rarely informative, omitted and named.
LOCKFILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "Cargo.lock", "poetry.lock", "uv.lock",
             "Pipfile.lock", "Gemfile.lock", "composer.lock", "go.sum", "flake.lock"}


def line_count(text):
    return text.count("\n") + int(bool(text) and not text.endswith("\n"))


def render(graph, warnings, max_lines=MAX_LINES):
    """The full source as plain text, or None when the capture exceeds the limits."""
    files, skipped = [], []
    for row in graph.body["source"]["files"]:
        if row["path"].rsplit("/", 1)[-1] in LOCKFILES:
            skipped.append(f"{row['path']} (lockfile)")
        elif row["text"] is None:
            skipped.append(f"{row['path']} ({row['state']})")
        else:
            files.append(row)
    lines = sum(line_count(r["text"]) for r in files)
    size = sum(len(r["text"].encode()) for r in files)
    if not files or not max_lines or lines > max_lines or size > MAX_BYTES:
        return None
    source = graph.body["source"]
    excluded = source["inventory"].get("exclusions", [])
    out = [f"WHOLE REPOSITORY: all {len(files)} readable captured files ({lines} lines) follow.",
           "Read them directly; graph queries add little at this size. `map --kind file` returns the JSON listing.",
           f"root: {source['root']}",
           f"head: {source['git'].get('head') or 'none'}",
           f"snapshot: {graph.snapshot}"]
    if skipped:
        out.append("not shown: " + ", ".join(skipped))
    if excluded:
        out.append(f"excluded by rules: {len(excluded)} paths (" + ", ".join(e["path"] for e in excluded[:10])
                   + (", ..." if len(excluded) > 10 else "") + ")")
    for w in warnings:
        if w["kind"] == "uncaptured_reference":
            refs = ", ".join(f"{r['path']}:{r['line']}" for r in w["examples"])
            out.append(f"not captured but used: {w['path']}/ (referenced at {refs})")
    for row in files:
        out += ["", f"==> {row['path']} <==", row["text"].removesuffix("\n")]
    return "\n".join(out) + "\n"
