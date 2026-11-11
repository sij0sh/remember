# Changelog

One entry per release. A release bumps `VERSION` and adds one entry here.

## [0.5.21] - unreleased

- DeepSeek Harness (dsh) support: `dsh` profile, `~/.dsh/sessions` recording root, `.jsonl.zstd` decoding (stdlib `compression.zstd`, `zstandard`, or the `zstd` CLI) and zstd-aware citation lookup.
- Workflow and recon tables load only when the legacy `_engine` package is installed; without it they are empty and `workflow` coverage reports `unavailable`.
- Lint: `ruff.toml` (py311, line 120, E4/E7/E9/F) and a CI lint job; the suite is clean under it.
- CI matrix now covers Python 3.11 and 3.14 on Ubuntu, macOS and Windows.
- Weekly Dependabot updates for pip.
- `.python-version` pins 3.14 for local development.
- Architecture overview in `references/architecture.md`.
- Parser tests skip when the optional tree-sitter extras are absent instead of failing.
