# Changelog

One entry per release. A release bumps `VERSION` and adds one entry here.

## [0.1.0] - unreleased

First release from the standalone repository, formerly part of the deprecated handtools bundle.

- Standard-library runtime; optional tree-sitter parsers pinned in `requirements.txt`.
- Workflow and recon tables load only when the legacy `_engine` package is installed; without it they are empty and `workflow` coverage reports `unavailable`.
- CI on Python 3.11 and 3.14: unittest suite plus `ruff check`; weekly Dependabot updates.
