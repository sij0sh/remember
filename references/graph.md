# Graph

`map`, `find`, `show` and the `inspect` relationships lane read one captured source graph. Nothing runs project code, installs packages or runs tests.

## map

- Bare `map` prints text: whole source up to 24 KB, else a directory summary. `--whole`: 3,000 lines, 200 KB.
- `--whole-repo-lines 0`, `--format json|table`, `--fields` give rows.
- Bare map text includes an orientation packet of about 2 KB: state of each view, best lead per view (shared with `investigate`), 3 non-merge commits and tree state; the current session is excluded.
- Other forms include the packet only with `--primer`. If a snapshot, byte cap or non-primer form omits it, `primer` says why.

## inspect

- `inspect --intent change` gives file-grain impact; a symbol target widens to its file (`scope.widened_targets`).
- Consumers have `reach`: `direct` (one hop, including a call, import line or mention naming a symbol), `file_only` (one hop, naming another part of the file) or `indirect` (2+ hops).
- Dependencies are `dependency_context`; tests are `verification` suggestions (`execution: not_run`). `risk_profile` counts each.
- Mentions outside reached files are `unresolved_mentions` gaps: lexical leads, not proven calls.
- `bug` and `review` walk both directions at the target's grain.
- `--depth`, `--max-nodes`, `--seconds` bound traversal; an omitted frontier stays in gaps.

| Predicate | Meaning |
| --- | --- |
| `CONTAINS`, `DECLARES` | Inventory containment and syntactic definitions. |
| `IMPORTS` | Import destination under a named resolver rule. |
| `CALLS_STATIC` | A supported Python, TS/JS or Rust call or construction. |
| `TESTS` | Test-file (or Rust `#[cfg(test)]`) import association, not assertion coverage. |
| `REFERENCES` | Literal path in a manifest or CI file (package.json, tsconfig, Cargo, workflows). Not an import; not traversed. |

- Traversal uses `IMPORTS` and `CALLS_STATIC`; tests never bridge unrelated code.
- Each row has `via` and one shortest witness.
- This is potential impact, not runtime reachability or proof of breakage.
- Python covers unambiguous function/class names and imported bindings. TS/JS and Rust follow supported re-exports and resolve methods when the receiver type is in scope. Unknown parameter types, ambiguous bindings, dynamic receivers, macros and trait dispatch stay unresolved.
- An unresolved import is a gap, not an absent dependency.

## Gaps

- A certain manifest path with no captured file: a `references` gap.
- `map` prints `referenced but missing` (not on disk) or `not captured but used` (ignored).
- Script and build-output folders are never reported missing.
- Generated or vendored files, listed: `dist`, `vendor`, `generated`; `.min.js`, `_pb2.py`, `.pb.go`, `.generated.*`; `@generated` or `DO NOT EDIT`; and `.d.ts` files of 1,000 lines or more.

## Capture

- One derived snapshot per workspace: source bytes, inventory, producer versions, graph and gaps under a content-addressed ID.
- Each read captures the tree; the snapshot is reused only when bytes and producer agree, else rebuilt and superseded.
- A superseded, expired, corrupt or older-schema snapshot ref fails; rerun the request.
- Workspaces, including separate worktrees, isolate caches. Idle ones are deleted after 14 days (swept at most daily on save; reads refresh age).
- With Git, inventory is tracked plus nonignored untracked paths; without Git, a directory walk that does not follow symlinks.
- Both skip: `.git`, `.hg`, `.svn`, `__pycache__`, `node_modules`, `.venv`, `venv`, `.remember`, `.pytest_cache`, `target`, `dist`, `build`.
- Add `--exclude GLOB` or root `.rememberignore` globs (one per line, `#` comments, no negation).
- Defaults: 20,000 files, 1 MiB/file, 32 MiB total.
- Binary or non-UTF-8 files are hashed, not parsed.
- Cache: `$XDG_CACHE_HOME/remember`, else `%LOCALAPPDATA%\remember`, else `~/.cache/remember`; must be outside the source root.

## find

- `find TEXT` is literal; `find --regex PATTERN` uses Python regex.
- `--word` and `--case-sensitive` apply to both.
- `--path` includes, `--not-path` excludes result paths: glob `*` crosses `/`, a plain path covers descendants. Filters do not change capture.
- `--count-by path` counts after filtering; `--kind occurrence` counts source lines only.

## Delivery

- `--limit` (30 rows; 200 for query), `--offset`, `--max-bytes` (12 KB) bound delivery only.
- Map and find pages replay on the pinned snapshot via `next`; `gaps` summarizes counts.
- Source reads take `--start-line` and `--lines`.
- Rows omit `id`, `key`, `digest` and default flags; `--verbose` restores them.
- `--fields` selects columns; an unknown name is an error.
- `--format table` prints columns, status and continuations.
