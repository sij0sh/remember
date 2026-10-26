# Query tools

## Graph SQL: SQLite

`remember.py query --sql SQL [--param NAME=VALUE]` queries one pinned graph.
Uses Python's SQLite; no installation needed. Single-quote string literals
(or use bound parameters); DuckDB-only functions are unavailable. `describe` lists table columns:

| Table | Grain |
|---|---|
| `nodes` | Directory, file, symbol or external import target. |
| `edges` | Typed relationship with producer, resolution, evidence and ref. |
| `files` | Captured inventory entry, digest and read state. |
| `gaps` | Unsupported or unresolved source/relationship occurrence. |

```bash
python3 "$S/remember.py" query --sql 'SELECT n.path, count(DISTINCT e.source) AS importing_files FROM edges e JOIN nodes n ON n.id=e.target WHERE e.predicate=:p GROUP BY n.path' --param p=IMPORTS
```

Only read-only SELECT/CTE statements are allowed. Source data and nested evidence
are JSON strings in SQL. Bound parameters are strings. Queries run on all captured
rows before delivery paging. Query delivery defaults to 200 rows; `--limit N`
raises delivery and retention together. The compatibility `--max-query-rows`
option sets a retention floor (default 1,000), raised to fit the requested page;
`--seconds` bounds execution. Capture gaps still apply to aggregates. Counts of
imports are not call counts or failure probabilities.

## Existing history/source SQL: DuckDB

`sessions.py select --sql SQL` queries normalized session events (`rows`, alias
`events`). `--include` adds current Git-backed repository views:

| Include | Views |
|---|---|
| `git` | `commits`, `changes`, `commit_links` |
| `files` | `files` |
| `symbols` | `code_items`, plus `files` |
| `content` | `files.content` |
| `edits` | `edit_hunks`, `edit_symbols`, plus symbols/files |

Queries reading only `commits`, `changes`, `files` and `code_items` skip recording
scans. These legacy source views require Git; the graph CLI supports ordinary
directories. The graph cache and legacy invocation-local views are separate.

```bash
python3 "$S/sessions.py" select --include symbols --sql 'SELECT path, qualname, kind FROM code_items LIMIT 20'
python3 "$S/sessions.py" select --include git --sql 'SELECT sha, subject FROM commits LIMIT 20'
python3 "$S/sessions.py" select --kind tool_call --sql 'SELECT tool, count(*) AS n FROM rows GROUP BY tool'
```

`--repo DIR` selects the worktree (default: `--project`, else the current directory). `--param NAME=VALUE` binds `$NAME`; `--type
VIEW.COLUMN=TYPE` declares empty/all-null columns. `describe` supplies exact
schemas. Joins can multiply witnesses; count distinct identities at the intended
grain. Commit witnesses distinguish recorded, inferred and unresolved links.
Edit-symbol mappings distinguish recorded-at-edit source from uniquely anchored
current source and unavailable correspondence.

`rows.py` composes supplied JSON envelopes as DuckDB views:

```bash
python3 "$S/rows.py" 'SELECT * FROM a LIMIT 10' --view 'a=python3 /absolute/scripts/sessions.py select'
```

It supports stdin, files and named producers. `--describe` lists input schemas.
Producer commands use argv parsing; `--shell` explicitly enables shell evaluation.
Each producer has a timeout/byte limit. Upstream clipped pages remain incomplete;
use `sessions.py select --sql` for aggregates over its full scanned selection.
