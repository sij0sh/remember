# Setup

- Python 3.11+, standard library only; SQL runs on stdlib SQLite.
- Optional tree-sitter packages (`requirements.txt`) add Rust, Go, TS/JS and shell definitions and imports; a missing grammar is reported per file, not installed.
- The macOS `/usr/bin/python3` from Xcode is 3.9: use Homebrew or python.org.
- `scripts/profiles` declares how harness JSONL becomes event rows; `settings.json` lists recording roots.
- dsh writes zstd JSONL (`~/.dsh/sessions/**/session.v4.jsonl.zstd`): stdlib `compression.zstd`, `zstandard` or the `zstd` CLI decodes it, else they are counted `unavailable`; refs use decompressed offsets. Its log is linear: file order supplies parents, a replacement `user/message` becomes one `surface_replacement` summary, other replacement copies are ignored.
- Unknown, malformed, oversized records stay visible as gaps; recorded branch/parent links kept where present.
