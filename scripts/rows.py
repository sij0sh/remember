#!/usr/bin/env python3
"""Query disposable JSON rows through an in-memory DuckDB engine. Writes nothing.

Usage: rows.py ['SELECT ...'] [INPUT ...]   (inputs default to stdin)

Inputs are files, '-' for stdin, or process substitution such as /dev/fd/N. Use
--view NAME=COMMAND to run a producer and register its stdout as a named view,
so joins across selections need no saved envelopes. Producers are parsed as
argv unless --shell is given explicitly.

Examples:
    sessions.py select --kind tool_call | rows.py "SELECT tool, count(*) AS n FROM rows GROUP BY 1"
    rows.py "SELECT ..." --view calls='sessions.py select --kind tool_call --fields tool,ref'
"""
import argparse
import os
import re
import selectors
import shlex
import signal
import subprocess
import sys
import time

from helper.bindings import parse_params, parse_types, types_for
from helper.engine import DEFAULT_MEMORY, DEFAULT_SECONDS, Input, evaluate
from helper.envelope import emit, load_rows

VIEW = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
DESCRIBE = {"usage": "rows.py ['SELECT ...'] [INPUT ...]; inputs default to stdin. --sql/--input remain aliases",
            "input": "files, '-' (stdin), or /dev/fd/N from process substitution; repeat for joins",
            "views": "--view NAME=COMMAND registers one producer's stdout as a disposable view: no file is written; "
                     "t0,t1,... are aliases and rows aliases the first view",
            "producer": "argv by default; --shell opts into shell evaluation. Every producer has a deadline and byte cap; "
                        "stderr is separated from the data stream",
            "columns": "scalar properties; objects/arrays become JSON strings; _row is a local ordinal",
            "engine": "in-memory DuckDB; spill, external files and extension loading are disabled",
            "queries": ["SELECT command, count(*) AS occurrences FROM rows GROUP BY command",
                        "SELECT ref->>'$.file' AS recording, count(*) AS n FROM rows GROUP BY recording"],
            "limits": "Input bytes/rows, memory, threads, query deadline and output rows are bounded",
            "partial": "status 'partial' means an input omitted rows or text, or reported a scan, query or source "
                       "limit (omissions.incomplete_reasons), so aggregates may undercount; prefer sessions.py "
                       "select --sql, which queries every matched row. Coverage-only inputs are rejected",
            "provenance": "Select ref for evidence rows; aggregates describe supplied rows only. "
                          "Re-query contributing rows to inspect support."}


def _kill(process):
    try:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def produce(command, max_bytes, seconds, shell=False):
    """Run a producer with a hard deadline and byte cap; never merge stderr into data."""
    argv = command if shell else shlex.split(command)
    if not argv:
        raise ValueError("Empty producer command")
    process = subprocess.Popen(argv, shell=shell, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    selector.register(process.stderr, selectors.EVENT_READ)
    data, errors = bytearray(), bytearray()
    deadline = time.monotonic() + seconds
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(f"Producer '{command}' exceeded --producer-seconds")
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.fileobj is process.stdout:
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ValueError(f"Producer '{command}' exceeded --max-input-bytes; narrow the selection")
                elif len(errors) < 16384:
                    errors.extend(chunk[:16384 - len(errors)])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError(f"Producer '{command}' exceeded --producer-seconds")
        code = process.wait(timeout=remaining)
    except Exception:
        _kill(process)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    if code:
        tail = errors.decode("utf-8", errors="replace")[-500:].strip()
        if not tail:
            tail = data.decode("utf-8", errors="replace")[-300:].strip()
        raise ValueError(f"Producer '{command}' failed ({code}): {tail or 'no output'}")
    if not data.strip():
        raise ValueError(f"Producer '{command}' produced no rows")
    return bytes(data)


def is_input(token):
    """A positional that names a readable file, node or stdin, unlike a query."""
    return token == "-" or os.path.exists(token)


def run(args):
    # A first positional that names an input is data; anything else is the query.
    positional = list(args.positional)
    sql = args.sql
    if positional and sql is None and not is_input(positional[0]):
        sql = positional.pop(0)
    sources = [(None, name) for name in args.input + positional]
    for definition in args.view:
        match = VIEW.match(definition)
        if not match or not match.group(2).strip():
            raise ValueError("Use --view NAME=COMMAND; NAME must be an identifier")
        sources.append((match.group(1), match.group(2)))
    if not sources:
        # --describe alone, or nothing piped at a terminal: describe rather than wait on stdin.
        if args.describe or sys.stdin.isatty():
            return {"results": [DESCRIBE]}
        sources = [(None, "-")]
    tables, remaining, total_rows = [], args.max_input_bytes, 0
    declared = parse_types(args.type, "rows")
    params = parse_params(args.param)
    for index, (name, source) in enumerate(sources):
        if name is not None:
            raw = produce(source, remaining, args.producer_seconds, args.shell)
        elif source == "-":
            raw = sys.stdin.buffer.read(remaining + 1)
        else:
            with open(source, "rb") as stream:
                raw = stream.read(remaining + 1)
        if len(raw) > remaining:
            raise ValueError("Input exceeds --max-input-bytes; narrow the selection or raise the limit")
        if not raw.strip():
            raise ValueError(f"Empty input: {source}")
        remaining -= len(raw)
        records, upstream = load_rows(raw.decode("utf-8"))
        total_rows += len(records) if isinstance(records, list) else 0
        if total_rows > args.max_input_rows:
            raise ValueError("Input exceeds --max-input-rows")
        types = types_for(declared, name or f"t{index}", f"t{index}", index == 0)
        tables.append(Input(name, records, upstream, None, types, "one input row"))
    return evaluate(tables, None if args.describe else (sql or "SELECT * FROM rows"),
                    args.limit, args.query_seconds, args.memory_limit,
                    params=params, grain="one input row")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("positional", nargs="*", metavar="SQL_OR_INPUT", help="Optional SELECT, then input files")
    p.add_argument("--input", action="append", default=[], help="Input file or '-' (repeat for joins)")
    p.add_argument("--view", action="append", default=[], metavar="NAME=COMMAND",
                   help="Run a producer command and register its stdout as view NAME")
    p.add_argument("--sql")
    p.add_argument("--describe", action="store_true")
    p.add_argument("--shell", action="store_true", help="Evaluate --view commands through a shell; argv is the default")
    p.add_argument("--limit", type=int, help="Output rows; defaults to the query's own LIMIT, capped at 1000")
    p.add_argument("--max-bytes", type=int, default=8192)
    p.add_argument("--max-input-bytes", type=int, default=16 * 1024 * 1024)
    p.add_argument("--max-input-rows", type=int, default=100000)
    p.add_argument("--query-seconds", type=float, default=DEFAULT_SECONDS)
    p.add_argument("--producer-seconds", type=float, default=120.0)
    p.add_argument("--memory-limit", default=DEFAULT_MEMORY)
    p.add_argument("--type", action="append", default=[], metavar="[VIEW.]COLUMN=TYPE",
                   help="Declare a column type: BIGINT, DOUBLE, VARCHAR, BOOLEAN, or JSON")
    p.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                   help="Bind a named query parameter ($name)")
    args = p.parse_args()
    if any(v <= 0 for k, v in vars(args).items() if isinstance(v, (int, float)) and not isinstance(v, bool)
           and (k.startswith("max_") or k in ("limit", "query_seconds", "producer_seconds"))):
        p.error("Limits must be positive")
    if args.sql is not None and args.positional and not all(is_input(x) for x in args.positional):
        p.error("Give the query once: positionally or with --sql")
    try:
        emit(run(args), args.max_bytes)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        emit({"status": "error", "results": [{"error": str(error)[:500]}]}, max(1024, args.max_bytes))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
