# Architecture

Status: draft, inferred from source. Confirm before relying on it.

`remember.py` is a thin entrypoint: it puts the repository root and `scripts/` on `sys.path` and calls
`helper.runtime.main`. Every command rebuilds one in-memory SQLite database from source, Git and recordings,
answers, then drops it. The only persisted state is one graph snapshot per repository in the cache.

## Flow

1. `operations.py` declares each CLI operation; `runtime.py` parses, plans and routes it.
2. `providers.py` loads only the providers a request names; `catalog.py` maps table to provider and orders them.
3. `engine.py` builds the disposable SQLite connection from `Input` rows; `projection.py` holds row schemas.
4. `envelope.py` wraps results with coverage and clipping; `delivery.py` pages and byte-fits the output.
5. `continuations.py` encodes a re-run descriptor. Nothing is written to the analyzed repository.

## Modules

| Group | Modules | Role |
|---|---|---|
| Entry and runtime | `remember.py`, `runtime.py`, `operations.py`, `options.py`, `budget.py` | Route an operation, enforce deadlines and budgets |
| Tables | `catalog.py`, `providers.py`, `engine.py`, `projection.py`, `bindings.py` | Declare tables, load providers lazily, execute SQL |
| Output | `delivery.py`, `envelope.py`, `continuations.py`, `cite.py`, `graph_output.py` | Bounded output, coverage, citations, next-page refs |
| Recordings | `discovery.py`, `reader.py`, `records.py`, `recording_scan.py`, `profiles.py`, `zstd.py`, `root_map.py` | Find and scan harness JSONL within caps; decode zstd |
| Recorded facts | `operation_facts.py`, `file_observations.py`, `edit_hunks.py`, `message_segments.py`, `summaries.py`, `reconciliation.py`, `error_signatures.py`, `episode_links.py` | Turn recorded events into typed, attributed facts |
| Investigations | `investigate.py`, `orientation.py`, `signals.py`, `trace.py`, `target_history.py`, `session_show.py`, `evidence_candidates.py`, `commit_rank.py`, `whole_repo.py` | Compose evidence packets and views |
| Source graph | `graph_store.py`, `graph_build.py`, `graph_queries.py`, `graph_cli.py`, `code_symbols.py`, `syntax_calls.py`, `import_targets.py`, `manifest_refs.py` | Capture one source snapshot and derive typed edges |
| Git | `git_resolve.py`, `git_file_history.py`, `git_commands.py`, `commit_links.py`, `repo_files.py` | Read-only Git resolution, history and worktree state |

## Invariants

- Runtime code is Python 3.11+ standard library only. Tree-sitter parsers are optional extras pinned in
  `requirements.txt`; when one is missing the graph reports `parser_unavailable`, never a silent gap.
- Nothing is written into an analyzed repository. The cache lives under `$XDG_CACHE_HOME/remember`.
- Recordings are untrusted data: text stays in the source file and results carry refs, coverage and gaps.
- Static edges are possible links; tests are candidates and are never executed.
- Each view reports `leads_found`, `none_found_in_scanned_scope`, `not_searched` or `unavailable`.
- Workflow and recon tables exist only when the deprecated `_engine` package is installed. Without it they
  are empty and `workflow` coverage is `unavailable`; there is no fallback journal reader.
