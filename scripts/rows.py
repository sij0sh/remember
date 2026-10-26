#!/usr/bin/env python3
"""Query disposable JSON rows through an in-memory DuckDB engine. Writes nothing.

Usage: rows.py ['SELECT ...'] [INPUT ...]   (inputs default to stdin)

Inputs are files, '-' for stdin, or (POSIX) process substitution such as /dev/fd/N. Use
--view NAME=COMMAND to run a producer and register its stdout as a named view,
so joins across selections need no saved envelopes. Producers are parsed as
argv unless --shell is given explicitly (sh on POSIX, cmd.exe on Windows). On
Windows, start producers with the interpreter: python sessions.py ...

Examples:
    sessions.py select --kind tool_call | rows.py "SELECT tool, count(*) AS n FROM rows GROUP BY 1"
    rows.py "SELECT ..." --view calls='sessions.py select --kind tool_call --fields tool,ref'
"""
import argparse
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time

from helper.bindings import parse_params, parse_types, types_for
from helper.engine import DEFAULT_MEMORY, DEFAULT_SECONDS, Input, evaluate
from helper.envelope import emit, load_rows

WINDOWS = os.name == "nt"
VIEW = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
DESCRIBE = {"usage": "rows.py ['SELECT ...'] [INPUT ...]; inputs default to stdin. --sql/--input remain aliases",
            "input": "files, '-' (stdin), or /dev/fd/N from POSIX process substitution; repeat for joins",
            "views": "--view NAME=COMMAND registers one producer's stdout as a disposable view: no file is written; "
                     "t0,t1,... are aliases and rows aliases the first view",
            "producer": "argv by default; --shell opts into shell evaluation (sh; cmd.exe on Windows). Every producer has a deadline and byte cap; "
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
    """Stop the producer and anything it started: its POSIX session or Windows process tree."""
    if process.poll() is None:
        try:
            if WINDOWS:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, timeout=5)
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _drain(stream, buffer, cap, overflow=None):
    """Read a pipe to EOF keeping at most cap bytes; with overflow, flag it and stop instead."""
    try:
        while chunk := os.read(stream.fileno(), 65536):
            if overflow is not None and len(buffer) + len(chunk) > cap:
                overflow.set()
                return
            buffer.extend(chunk[:max(0, cap - len(buffer))])
    except (OSError, ValueError):
        pass


def produce(command, max_bytes, seconds, shell=False):
    """Run a producer with a hard deadline and byte cap; never merge stderr into data."""
    # Windows parses its own command line; POSIX splitting would eat backslashes in paths.
    if not command.strip():
        raise ValueError("Empty producer command")
    argv = command if shell or WINDOWS else shlex.split(command)
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}
    process = subprocess.Popen(argv, shell=shell, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, **group)
    # Reader threads rather than selectors: Windows cannot select on pipes.
    data, errors, overflow = bytearray(), bytearray(), threading.Event()
    readers = [threading.Thread(target=_drain, args=(process.stdout, data, max_bytes, overflow), daemon=True),
               threading.Thread(target=_drain, args=(process.stderr, errors, 16384), daemon=True)]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + seconds
    try:
        while any(reader.is_alive() for reader in readers):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(f"Producer '{command}' exceeded --producer-seconds")
            if overflow.wait(min(remaining, 0.05)):
                raise ValueError(f"Producer '{command}' exceeded --max-input-bytes; narrow the selection")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError(f"Producer '{command}' exceeded --producer-seconds")
        code = process.wait(timeout=remaining)
    except Exception:
        _kill(process)
        raise
    finally:
        for reader in readers:
            reader.join(2)
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
