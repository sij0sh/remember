"""In-process CLI invocation for contract tests.

`invoke` calls the same `runtime.main` the entrypoint calls, capturing exit code,
stdout and stderr. It exists so a test can assert CLI behavior without paying a
new interpreter per call. Use `subprocess` directly for process-level contracts
(signals, memory caps, the installed entrypoint). Because the call is in-process,
module patches are visible to it; do not combine a patch with this helper unless
that is the behavior under test.
"""
import contextlib
import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from helper import cite, discovery, runtime  # noqa: E402

SCRIPT = str(ROOT / "scripts" / "remember.py")


class Output:
    """Text stream with the byte buffer `delivery.write` expects."""

    def __init__(self):
        self.buffer = io.BytesIO()

    def write(self, text):
        return self.buffer.write(text.encode("utf-8"))

    def flush(self):
        pass

    def value(self):
        return self.buffer.getvalue().decode("utf-8", "replace")


class Result:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def json(self):
        return json.loads(self.stdout)


def invoke(argv, stdin=None, env=None, cwd=None):
    """Run one CLI request in this process and capture its file-descriptor-free output."""
    argv = [str(value) for value in argv]
    out, err = Output(), io.StringIO()
    settings = (Path(env["TRACE_SESSIONS_SETTINGS"])
                if env and env.get("TRACE_SESSIONS_SETTINGS") else discovery.SETTINGS)
    stream = io.TextIOWrapper(io.BytesIO((stdin or "").encode("utf-8")), encoding="utf-8")
    saved = {name: os.environ.get(name) for name in (env or {})}
    try:
        for name, value in (env or {}).items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        # Discovery and cite resolution read their inputs once per process; a test may
        # point them at a different archive between calls.
        cite._recording.cache_clear()
        with mock.patch.object(discovery, "SETTINGS", settings), \
                mock.patch.object(sys, "argv", [SCRIPT, *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", stream), \
                contextlib.chdir(cwd) if cwd else contextlib.nullcontext():
            try:
                code = runtime.main(argv)
            except SystemExit as exit:
                code = exit.code if isinstance(exit.code, int) else 2
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return Result(code, out.value(), err.getvalue())
