#!/usr/bin/env python3
"""remember entrypoint; the shared workflow owner is imported without invoking a skill."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from helper.runtime import main  # noqa: E402  (the entrypoint inserts the repository root first)

if __name__ == "__main__":
    raise SystemExit(main())
