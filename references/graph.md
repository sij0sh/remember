# Graph tools

`python3 "$S/remember.py" COMMAND --repo DIR` returns JSON by default, except bare `map`: whole source below 3,000 lines
(and 200 KB), otherwise a directory summary. `--whole-repo-lines 0`, explicit
`--format json|table`, or `--fields` selects rows. No command executes
project code, installs dependencies or runs tests. Targeted graph commands do
not scan recordings. The documented exception is bare `map` text, which reads a
bounded recent-work primer: 3 sessions, 3 non-merge commits, current session
excluded, about 2 KB. Use `--no-primer`, `--primer-depth N`, `--primer-bytes N`,
`--primer-root DIR`, `--primer-merges`, or `--exclude-session ID`. Other `map`
forms include it only with `--primer` (JSON field or table lines); otherwise, and
under `--snapshot` or a byte cap, `primer` names the omission. Targeted
results provide `history.next`; explicit `history TARGET` retrieval reads
recordings (see [history](history.md)).
`describe` derives the command/argument catalog from the same parser as the CLI.

## Examples

```bash
python3 "$S/remember.py" map --kind symbol
python3 "$S/remember.py" find --regex 'retry|timeout' --path 'src/*' --not-path '*.d.ts' --count-by path
python3 "$S/remember.py" analyze fanin --format table
python3 "$S/remember.py" analyze orphans --grain symbol --exclude-public
python3 "$S/remember.py" neighbors src/store.py --direction in --depth 3
python3 "$S/remember.py" neighbors 'src/store.py::save' --grain symbol --predicate CALLS_STATIC
python3 "$S/remember.py" paths src/api.py src/store.py --depth 4
python3 "$S/remember.py" impact src/store.py
python3 "$S/remember.py" snapshot
python3 "$S/remember.py" impact --base SNAPSHOT_A --head SNAPSHOT_B
python3 "$S/remember.py" show RETURNED_REF
python3 "$S/remember.py" query --sql 'SELECT target, count(DISTINCT source) AS consumers FROM edges WHERE predicate = :kind GROUP BY target' --param kind=IMPORTS
```

Use `--snapshot ID` for an existing view. Otherwise each read captures current
source, reusing an unchanged capture's graph. File-level traversal is the default;
`--grain symbol` traverses the original graph. Directory targets expand to files;
`impact` runs at file grain, takes no `--grain`, and reports symbol targets in
`scope.widened_targets`. Consumer rows carry `reach`: `direct` (one hop; for a
symbol target, a call into it, an import line or a mention naming it),
`file_only` (one hop, names only other parts of the file) or `indirect` (two or
more hops); `risk_profile` counts each.

Delivered rows omit `id`, `key`, `digest`, `parse_state: parsed`, `state: read`
and false flags; witness steps keep `predicate` and `edge`. `--verbose` returns
full rows. `analyze` defaults to 60 rows / 24 KB per page, others 30 / 12 KB.
Ambiguous names return candidates instead of selecting one silently.

## Evidence and scope

| Predicate | Meaning |
|---|---|
| `CONTAINS`, `DECLARES` | Inventory containment and syntactic definitions. |
| `IMPORTS` | Import destination under the named resolver rule. |
| `CALLS_STATIC` | A supported Python, TS/JS or Rust call or construction. |
| `TESTS` | Test-file (or Rust `#[cfg(test)]` item) import association, not verified assertion coverage. |
| `REFERENCES` | Manifest/CI literal path: package.json scripts, main/types/bin/exports/files; tsconfig extends/include/files/references; Cargo lib/bin/test/build/path deps/workspace members; CI workflow lines. Not an import; not traversed by default. |

A certain path (tsconfig field, Cargo path, path-shaped script argument) that
matches no captured file is a `references` gap; `map`/`status` print
`referenced but missing: PATH` when it is absent on disk, or `not captured but
used` when ignored. Script outputs (`--outfile`, `>`) and build-output folders
(`dist`, `build`, `target`, ...) are never reported missing.

Default dependency traversal uses `IMPORTS` and `CALLS_STATIC`. Incoming and
outgoing impact traversals remain separate. Tests are attached as suggestions;
they never bridge unrelated implementations. Each returned traversal result
includes one shortest witness under the chosen direction/predicates. This is
potential impact, not runtime reachability or proof of breakage.

Python calls cover unambiguous function/class names and imported bindings.
TS/JS and Rust follow supported re-exports and resolve methods when a receiver's
class/type is known in the same scope. TS/JS `new X()` and Python `Cls()` link
to the class. Rust follows `pub use` and constructors returning `Self`, including
supported Result/Option wrappers. Parameters without a known type, ambiguous
bindings, dynamic receivers, macros and trait dispatch remain unresolved.
These rules do not constitute general type inference or runtime reachability.

Incoming symbol queries expose unresolved name mentions with source sites.
An empty result with such mentions reports `status: unknown`; nonempty incomplete
results report `partial`. Mentions are lexical leads, potentially comments or
unrelated names, not proven calls. Impact lists mentions outside reached files.
An unsuccessful path search with import/call gaps or target mentions reports
`scope.path_result: no_path_gaps_present`. Traversal rows carry `via` and a
witness path; a zero-length path has no edge witness.

## Search and analyses

`find TEXT` is literal; `find --regex PATTERN` uses Python regex syntax.
`--word` and `--case-sensitive` apply to both. Legacy `find TEXT --regex` works.
Repeat `--path` to include and `--not-path` to exclude result paths: glob `*`
crosses `/`, while plain paths include descendants. These filters do not change
capture; `--exclude` does. `--count-by path` counts matching result rows after
kind/path filtering; use `--kind occurrence` to count source lines alone.

`analyze fanin|orphans|cycles|untested|largest` uses the pinned graph:

| Report | Meaning |
|---|---|
| `fanin` | Distinct file consumers or symbol callers. |
| `orphans` | No inbound links, with entrypoint/mention hints (`manifest_reference` + `referenced_by` when a manifest names the file); not proof of dead code. |
| `cycles` | File dependency strongly connected components with edge witnesses. |
| `untested` | No direct TESTS association; import reachability from tests to depth 3. Files with Rust `#[cfg(test)]`/`#[test]` items are skipped (`scope.inline_tested`). Not execution coverage. |
| `largest` | Source lines per symbol (default) or file. |

Tests are excluded by default; `--include-tests` includes them where applicable
(`untested` always reports implementation files). `--grain file|symbol` (alias
`--kind`) applies to fanin, orphans and largest; cycles and untested are file-only.
`--exclude-entrypoints` filters naming/main-guard candidates (cycles containing
one are omitted). `--exclude-public` filters top-level non-underscore candidates
and requires symbol grain. These are heuristics, not export or entrypoint proof.
Generated/vendored files are left out of every report and listed in
`scope.excluded_generated`: a `dist`, `vendor`, `third_party`, `generated` or
similar folder; `.min.js`, `_pb2.py`, `.pb.go`, `.generated.*` names; an
`@generated`/`DO NOT EDIT`/`regenerate with` header; `.d.ts` of 1,000+ lines.
`--include-generated` ranks them.

Definition/import extraction: Python AST; optional tree-sitter for Rust, Go,
TS/JS and shell. Python import roots include the repository root, `src`, package
parent and importing directory; multiple matches are ambiguous. Rust walks module
files and Cargo package names; Go reads `go.mod` and package files. TS/JS resolves
relative specifiers, the nearest `tsconfig.json` `paths`/`baseUrl` (following
relative `extends`), then workspace `package.json` names through `exports`,
`main` or `types`, mapping `outDir` builds back to `rootDir` source. Other bare
specifiers are `not_in_repo`; `@/`, `~/` and `#` aliases without a mapping stay
unresolved. Relative shell sources are unresolved because runtime working
directory is unknown. Build features, bundler aliases, Rust `#[path]`,
environment import paths, Go replacements and installed packages are not modeled.
Import gaps are one row per statement, with `bindings` counting its names.
External packages are `IMPORTS` edges to external nodes, not gaps; non-code files
are counted in `coverage.unparsed_files`, not gaps. Resolution is scoped to
these rules, not compiler certainty. Unsupported parsers/files remain visible.

Top-level non-underscore symbol candidates are a compatibility prompt, not an export
contract. Responsibility, state lifetime and architectural ownership are not
inferred from fan-in, naming or graph distance. The risk profile reports evidence
and unknowns, without a correctness score.

## Snapshots and comparison

Source bytes, inventory, producer versions, graph and gaps are retained under
content-addressed IDs. Workspace roots isolate caches, including separate Git
worktrees. Configuration/manifests are captured as source; a new digest causes
re-extraction/resolution. Captures include untracked and dirty files and tracked
missing paths. Files changing during capture are marked unavailable.

Comparison examines the full inventory on both sides. Deleted targets retain
baseline consumers. Missing files in an incomplete capture do not prove deletion.
Relationship differences mean observed only on one side, not established semantic
additions/removals. Equal-content correspondence is separate from identity;
ambiguous moves are not guessed and no correspondence remaps the graph.

Pinned reads report current freshness separately; `show` always reads retained
bytes. A later edit cannot change an earlier source reference. No automatic cache
eviction occurs. Deleting the cache expires its references; unavailable/corrupt
references return errors rather than current-source substitutes.

## Capture, limits and delivery

With Git available, inventory uses tracked and nonignored untracked paths.
Without Git, it walks the directory without following symlinks. Both modes exclude
`.git`, `.hg`, `.svn`, `__pycache__`, `node_modules`, `.venv`, `venv`, `.remember`,
`.pytest_cache`, `target`, `dist` and `build`; exclusions are recorded. Add
`--exclude GLOB` or root `.rememberignore` globs (one per line, `#` comments, no
negation). Component globs match at any depth; slash-containing globs match
root-relative paths. Directory mode does not interpret `.gitignore`.

Defaults: 20,000 files; 1 MiB/file; 32 MiB total. Non-UTF-8/binary files are hashed
but not parsed. Oversize/unreadable files remain inventory entries. Cache defaults
to `$XDG_CACHE_HOME/remember`, else `%LOCALAPPDATA%\remember` on Windows, else
`~/.cache/remember`; `--cache DIR` must be outside
the source root. The retained evidence contains source text; no transcript import
or automatic upload is performed.

Traversal bounds: `--depth`, `--max-nodes`, `--seconds`. Omitted frontiers stay in
the retained scope/gaps. Delivery bounds: `--limit`, `--offset`, `--max-bytes`.
They do not limit capture/traversal. Results are retained before delivery (query retention defaults to 1,000 rows);
follow `report`/`next`, or `show REPORT/gaps`. Source reads support `--start-line`
and `--lines`. Increase the byte budget if a single result row does not fit.

`--limit` defaults to 30 rows (query: 200) and raises query retention to fit the
requested page. `--max-query-rows` remains a compatibility option; `--limit` can
raise it. `results_truncated` reports delivery or query retention clipping;
raise `--max-bytes` when bytes, rather than row count, are the bottleneck.
`--format table` prints selected columns plus status, gaps and continuations.
`--fields path,line,via` projects delivered rows in either format, without
changing retained evidence. `show REPORT` can recover the full columns.

Exit 0 means the operation completed, including explicitly partial evidence.
`status: partial` comes from inventory, capture, source, parser, traversal, query or mention
gaps, or undelivered rows; import, call and target gaps leave it `ok`.
An empty incoming result with target mentions is `unknown`.
Exit 2 means invalid input or an operation failure. `status`, `coverage`, `gaps`
and `delivery` describe independent limitations. No result certifies safety.
