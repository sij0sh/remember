# Setup and limits

The graph CLI (`remember.py`) needs Python 3.11+ and only the standard library.
Python AST extraction is built in. Optional tree-sitter packages add syntax/import
extraction for Rust, Go, TypeScript/JavaScript and shell; missing grammars are
reported, not installed automatically.

`sessions.py` scanning/search uses Python 3.9+. Legacy SQL, `duplicates` and
`rows.py` require Python 3.11+, DuckDB and PyArrow. Install `requirements.txt` in
a virtual environment when those optional capabilities are needed:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt          # Linux/macOS
py -3 -m venv .venv; .venv\Scripts\python -m pip install -r requirements.txt   # Windows
```

macOS: the Xcode `/usr/bin/python3` is 3.9; use Homebrew or python.org 3.11+.
Windows: `rows.py` process substitution (`<(...)`) is unavailable; use `--view`.

All commands expose `--help`. Graph schemas come from `remember.py describe`;
historical event/view schemas from `sessions.py describe`. SQL dialects differ:
graph queries use SQLite; legacy history/composition queries use DuckDB.

Graph source, cache, parser and traversal limits: [graph.md](graph.md).
Historical source profiles live in `scripts/profiles`; recording roots in
`settings.json`. Profiles normalize Codex, Claude Code and pi JSONL. Unknown,
malformed and oversized records remain visible as gaps; recorded branch/parent
links are preserved where available. Text may be omitted by scan or output budgets.

Bare `map` uses the same recording profiles for a recent-work primer. It reads
at most 1 MB from the head and 8 MB from the tail of a large recording, keeps
only normalized events, and caps text per file. Long sessions are therefore
sampled, not fully scanned; `coverage.sessions.sampled` is true. The primer is
printed, never saved with the report. Controls and field details are in
[history](history.md).

Historical output is one JSON envelope. `scope` records the scanned selection;
`omissions` records missing rows/inputs; `detail` records display clipping; `next`
provides continuations. SQL runs before output paging, but input scan/text limits
still apply. Non-graph derived history data is invocation-local.
