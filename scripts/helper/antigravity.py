"""Antigravity session path rules, read-only SQLite metadata lookup, and output adapters.

Antigravity writes canonical full transcripts to
``<appDataDir>/brain/<conversation-uuid>/.system_generated/logs/transcript_full.jsonl``
alongside compact/chunked copies (``transcript.jsonl``, ``chunks/``). Workspace roots and
parent-session links live in sibling ``<appDataDir>/conversation_summaries.db`` (WAL-mode
SQLite, read strictly read-only) with ``<appDataDir>/history.jsonl`` as fallback.
"""
import json
import os
import re
import sqlite3
import urllib.parse
from functools import lru_cache
from pathlib import Path

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
EXIT_CODE_RE = re.compile(r"^Command exited with code (-?\d+)\.\s*$")
SHOWING_RE = re.compile(r"^Showing lines (\d+)-(\d+) of (\d+)\s*$")
LINE_RE = re.compile(r"^(\d+): ?(.*)$")
PREAMBLE_RE = re.compile(r"^(?:Created|Completed) At: ")
TRUNCATION_MARKER = "The tool's output was truncated due to being too long."


def conversation_id(path):
    """Extract <uuid> from .../<uuid>/.system_generated/logs/<file>.jsonl, else None."""
    p = Path(path)
    if p.parent.name != "logs" or len(p.parents) < 3 or p.parents[1].name != ".system_generated":
        return None
    found = UUID_RE.findall(p.parents[2].name)
    return found[-1] if found else None


def is_noncanonical(path):
    """True for Antigravity compact/chunked duplicate logs under .system_generated/logs/."""
    p = Path(path)
    if p.name == "transcript.jsonl" and len(p.parents) >= 2 and p.parent.name == "logs" and p.parents[1].name == ".system_generated":
        return True
    parts = p.parts
    for i in range(len(parts) - 2):
        if parts[i] == ".system_generated" and parts[i + 1] == "logs" and parts[i + 2] == "chunks":
            return True
    return False


def uri_to_path(uri):
    """Convert a file:// URI (POSIX, Windows drive, UNC, percent-encoded) to a filesystem path."""
    if not isinstance(uri, str) or not uri:
        return None
    if not uri.startswith("file://"):
        return uri
    parsed = urllib.parse.urlparse(uri)
    raw = urllib.parse.unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        return f"//{parsed.netloc}{raw}"
    if re.match(r"^/[A-Za-z]:/", raw):
        raw = raw[1:]
    return raw or None


def _app_data_dir(path):
    """<appDataDir> for <appDataDir>/brain/<uuid>/.system_generated/logs/<file>.jsonl, else None."""
    p = Path(path).absolute()
    if (p.parent.name == "logs" and len(p.parents) >= 5
            and p.parents[1].name == ".system_generated" and p.parents[3].name == "brain"):
        return p.parents[4]
    return None


def _stat_sig(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


@lru_cache(maxsize=32)
def _load_summaries_db(db_str, db_sig, wal_sig):
    """Read-only snapshot of conversation_summaries.db: {conversation_id: (roots, parent_id)}."""
    del db_sig, wal_sig
    base_uri = Path(db_str).as_uri()
    con = None
    for query in ("?mode=ro", "?mode=ro&immutable=1"):
        try:
            con = sqlite3.connect(base_uri + query, uri=True, timeout=1.0)
            con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            break
        except sqlite3.Error:
            if con is not None:
                con.close()
                con = None
    if con is None:
        return {}
    out = {}
    try:
        try:
            rows = con.execute(
                "SELECT conversation_id, workspace_uris, parent_conversation_id FROM conversation_summaries"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for cid, uris_raw, parent in rows:
            if not isinstance(cid, str) or not cid:
                continue
            roots = []
            if isinstance(uris_raw, str) and uris_raw:
                try:
                    parsed = json.loads(uris_raw)
                except ValueError:
                    parsed = []
                if isinstance(parsed, list):
                    for uri in parsed:
                        converted = uri_to_path(uri)
                        if converted and converted not in roots:
                            roots.append(converted)
            parent_id = parent if isinstance(parent, str) and parent else None
            out[cid] = (tuple(roots), parent_id)
        try:
            parents = con.execute("SELECT conversation_id, parent_id FROM conversation_parents").fetchall()
        except sqlite3.Error:
            parents = []
        for cid, parent in parents:
            if not isinstance(cid, str) or not cid or not isinstance(parent, str) or not parent:
                continue
            existing_roots, existing_parent = out.get(cid, ((), None))
            if not existing_parent:
                out[cid] = (existing_roots, parent)
    finally:
        con.close()
    return out


@lru_cache(maxsize=32)
def _load_history_jsonl(hist_str, hist_sig):
    """Fallback {conversation_id: workspace} from <appDataDir>/history.jsonl."""
    del hist_sig
    out = {}
    try:
        with open(hist_str, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(item, dict):
                    continue
                cid = item.get("conversationId")
                ws = item.get("workspace")
                if isinstance(cid, str) and cid and isinstance(ws, str) and ws:
                    out[cid] = uri_to_path(ws) or ws
    except OSError:
        return {}
    return out


def session_metadata(path):
    """{session, roots, root, parent_session} from Antigravity path and sibling read-only DB."""
    cid = conversation_id(path)
    roots = []
    parent_session = None
    app_dir = _app_data_dir(path) if cid else None
    if app_dir is not None:
        db_path = app_dir / "conversation_summaries.db"
        db_sig = _stat_sig(db_path)
        if db_sig is not None:
            wal_sig = _stat_sig(str(db_path) + "-wal")
            entry = _load_summaries_db(str(db_path), db_sig, wal_sig).get(cid)
            if entry is not None:
                roots = list(entry[0])
                parent_session = entry[1]
        if not roots:
            hist_path = app_dir / "history.jsonl"
            hist_sig = _stat_sig(hist_path)
            if hist_sig is not None:
                ws = _load_history_jsonl(str(hist_path), hist_sig).get(cid)
                if ws:
                    roots = [ws]
    return {"session": cid, "roots": roots, "root": roots[0] if roots else None,
            "parent_session": parent_session}


def extract_exit_code(text):
    """Command exit code from Antigravity run_command preamble (before Command output:), else None."""
    if not isinstance(text, str) or not text:
        return None
    for line in text.splitlines()[:15]:
        if line == "Command output:":
            break
        match = EXIT_CODE_RE.match(line)
        if match:
            return int(match.group(1))
    return None


def command_result(exit_code, status_outcome):
    """(exit_code, outcome) for a GENERIC result paired with run_command."""
    if status_outcome == "failed":
        return exit_code, "failed"
    if exit_code == 0:
        return 0, "succeeded"
    if exit_code is not None:
        return exit_code, "failed"
    return None, "unknown"


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def read_view(text, arguments):
    """{content, start_line, line_count, total_lines, completeness, adapter} or None for read_file."""
    if not isinstance(text, str) or not text:
        return None
    lines = text.split("\n")
    if lines and not lines[-1]:
        lines.pop()
    index = 0
    while index < len(lines) and (PREAMBLE_RE.match(lines[index]) or not lines[index].strip()):
        index += 1
    if index >= len(lines):
        return None
    if lines[index].startswith(TRUNCATION_MARKER):
        return None
    header = SHOWING_RE.match(lines[index])
    if not header:
        return None
    start, end, total = int(header.group(1)), int(header.group(2)), int(header.group(3))
    body = lines[index + 1:]
    matched = [LINE_RE.match(line) for line in body]
    if not matched or not all(matched):
        return None
    expected = end - start + 1
    if len(matched) != expected or int(matched[0].group(1)) != start or int(matched[-1].group(1)) != end:
        return None
    arguments = arguments if isinstance(arguments, dict) else {}
    byte_offset = _int(arguments.get("ByteOffset"))
    ranged = any(_int(arguments.get(k)) is not None for k in ("StartLine", "EndLine", "ByteOffset"))
    complete = start == 1 and end == total and not byte_offset
    completeness = "complete" if complete else "range" if ranged else "truncated"
    return {"content": "\n".join(m.group(2) for m in matched), "start_line": start,
            "line_count": len(matched), "total_lines": total,
            "completeness": completeness, "adapter": "antigravity:read_file"}
