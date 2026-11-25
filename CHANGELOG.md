# Changelog

One entry per release. A release bumps `VERSION` and adds one entry here.

## [0.5.23] - unreleased

- Google Antigravity support: `antigravity` profile (v1) for `transcript_full.jsonl` under `~/.gemini/antigravity-cli/brain` and `~/.gemini/antigravity/brain`, added to `settings.json` and the default recording roots.
- Linear `step-N` ancestry; tool results pair positionally with the preceding `PLANNER_RESPONSE.tool_calls`; dispatch `exit_code` is read from `Command exited with code N` and status decides the outcome.
- `read_file`/`edit_file`/`write_file` adapters: `Showing lines a-b of n` ranges, `Target`/`Replacement` hunks, `Content` writes and `Append` edits; exit-code and range markers never leak into payload text.
- Compact `transcript.jsonl` and `chunks/` duplicates are skipped during discovery and citation lookup; workspace roots, multi-root and parent-session links come read-only from sibling `conversation_summaries.db` (WAL-aware static open), with `history.jsonl` as fallback.
- `ANTIGRAVITY_CONVERSATION_ID` excludes the current session; `<user_request>` is prose while `additional_metadata`, `user_settings_change` and `system_message` are harness context.

## [0.5.22] - unreleased

- Merge `origin/main`: lint job with `ruff.toml`, CI matrix, Dependabot, architecture overview and workflow tables from the shared foundation.
- `references/history.md` keeps the compact lane wording over the restored upstream copy; `.agents` artifacts and state directories stay untracked.

## [0.5.21] - unreleased

- DeepSeek Harness (dsh) support: `dsh` profile, `~/.dsh/sessions` recording root, `.jsonl.zstd` decoding (stdlib `compression.zstd`, `zstandard`, or the `zstd` CLI) and zstd-aware citation lookup.
- Workflow and recon tables load only when the legacy `_engine` package is installed; without it they are empty and `workflow` coverage reports `unavailable`.
- Lint: `ruff.toml` (py311, line 120, E4/E7/E9/F) and a CI lint job; the suite is clean under it.
- CI matrix now covers Python 3.11 and 3.14 on Ubuntu, macOS and Windows.
- Weekly Dependabot updates for pip.
- `.python-version` pins 3.14 for local development.
- Architecture overview in `references/architecture.md`.
- Parser tests skip when the optional tree-sitter extras are absent instead of failing.
