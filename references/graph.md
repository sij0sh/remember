# Graph tools

`python3 "$S/remember.py" COMMAND --repo DIR` returns JSON. No command executes
project code, installs dependencies, runs tests or discovers/imports session archives.
`describe` derives the command/argument catalog from the same parser as the CLI.

## Examples

```bash
python3 "$S/remember.py" map --kind symbol
python3 "$S/remember.py" find 'retry'
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
`impact` runs at file grain and reports symbol targets in `scope.widened_targets`.
Ambiguous names return candidates instead of selecting one silently.

## Evidence and scope

| Predicate | Meaning |
|---|---|
| `CONTAINS`, `DECLARES` | Inventory containment and syntactic definitions. |
| `IMPORTS` | Import destination under the named resolver rule. |
| `CALLS_STATIC` | A Python, TS/JS or Rust call with one supported lexical function binding. |
| `TESTS` | Test-file import association, not verified assertion coverage. |

Default dependency traversal uses `IMPORTS` and `CALLS_STATIC`. Incoming and
outgoing impact traversals remain separate. Tests are attached as suggestions;
they never bridge unrelated implementations. Each returned traversal result
includes one shortest witness under the chosen direction/predicates. This is
potential impact, not runtime reachability or proof of breakage.

Python calls cover unambiguous direct function names and imported module/function
bindings. Parameters, rebinding, star imports, dynamic receivers, lambdas and
comprehensions remain unresolved. TS/JS and Rust calls resolve a name bound
exactly once in its file (any declaration, parameter or pattern counts) to a
function declared there or imported from a resolved file; TS follows
`export { x } from` re-exports, and `this.m()`, `self.m()`, `Self::f()`,
`Type::f()`, `ns.f()` and Rust module paths resolve within the declaring file.
Default/require imports, closures, macros, constructors and trait dispatch stay
unresolved. Their call gaps are one row per file and reason, with `calls` as the
count. No type inference, framework wiring or external-client enumeration is claimed.

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
to `$XDG_CACHE_HOME/remember` or `~/.cache/remember`; `--cache DIR` must be outside
the source root. The retained evidence contains source text; no transcript import
or automatic upload is performed.

Traversal bounds: `--depth`, `--max-nodes`, `--seconds`. Omitted frontiers stay in
the retained scope/gaps. Delivery bounds: `--limit`, `--offset`, `--max-bytes`.
They do not limit capture/traversal. Full results are retained before delivery;
follow `report`/`next`, or `show REPORT/gaps`. Source reads support `--start-line`
and `--lines`. Increase the byte budget if a single result row does not fit.

Exit 0 means the operation completed, including explicitly partial evidence.
`status: partial` comes from inventory, capture, source, parser, traversal or query
gaps, or undelivered rows; import, call and target gaps leave it `ok`.
Exit 2 means invalid input or an operation failure. `status`, `coverage`, `gaps`
and `delivery` describe independent limitations. No result certifies safety.
