"""Short citation tokens for expanded evidence, and a read-only lint for the citations in a report."""
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from .discovery import default_recordings
from .records import new_scope, records, safe_ref

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# <recording id>@<line>[.<item>]#<first 12 hex of the record's sha256>
TOKEN = re.compile(r"(?P<id>[A-Za-z0-9][A-Za-z0-9_-]*)@(?P<line>\d+)(?:\.(?P<item>\d+))?#(?P<hash>[0-9a-f]{12})\b")
FULL_SHA = re.compile(r"\b[0-9a-f]{40}\b")
SESSION = re.compile(rf"\b{UUID}\b")
# A hex run that could be a shortened commit, session or hash: at least one digit and one letter.
BARE_HEX = re.compile(r"\b(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,39}\b")
LONG_HEX = re.compile(r"\b[0-9a-f]{41,}\b")


def recording_id(file):
    """The session id a recording's file name carries (pi, codex, claude-code), else its stem."""
    name = Path(file).name
    found = re.findall(UUID, name)
    return found[-1] if found else Path(name).stem


def token(ref):
    """Citable form of an expanded ref; resolve it with --ref TOKEN."""
    item = ref.get("item", 0)
    return f"{recording_id(ref['file'])}@{ref['line']}{f'.{item}' if item else ''}#{ref['sha256'][:12]}"


@lru_cache(maxsize=None)
def _recording(rid):
    found = []
    for paths in default_recordings().values():
        for root in paths:
            if root.is_dir():
                found.extend(p for p in root.rglob("*.jsonl") if not p.is_symlink() and recording_id(p) == rid)
    unique = sorted({str(p.absolute()) for p in found})
    if not unique:
        raise ValueError(f"No recording named {rid} under the configured recordings; pass the JSON ref instead")
    if len(unique) > 1:
        raise ValueError(f"{len(unique)} recordings are named {rid}; pass the JSON ref instead")
    return unique[0]


def resolve(text, max_record=2 * 1024 * 1024):
    """A ref for a cite token: the recorded line, verified against the token's hash."""
    m = TOKEN.fullmatch(text.strip())
    if not m:
        raise ValueError("Not a cite token")
    path, line = _recording(m["id"]), int(m["line"])
    scope = new_scope()
    for ref, _, _ in records(path, scope, max_record, 1 << 62):
        if ref["line"] == line:
            if not ref["sha256"].startswith(m["hash"]):
                raise ValueError("Source changed at this cite token; search again")
            return {**ref, "item": int(m["item"] or 0), "pointer": ""}
    raise ValueError(f"{m['id']} has no line {line}")


def ref_argument(value, max_record=2 * 1024 * 1024):
    """--ref accepts a returned JSON ref or a cite token; a bare id or prefix is refused."""
    text = value.strip() if isinstance(value, str) else value
    if isinstance(text, str) and not text.startswith("{"):
        if TOKEN.fullmatch(text):
            return resolve(text, max_record)
        raise ValueError("--ref takes a returned JSON ref or a cite token (id@line#hash) from context, edges, "
                         "payload or raw; a bare session id or prefix is not a ref")
    return safe_ref(text)


def _commit(repo, sha):
    try:
        result = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", sha + "^{commit}"],
                                capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unchecked"
    return "ok" if result.returncode == 0 else "not_in_repo"


def lint(paths, repo, max_record=2 * 1024 * 1024):
    """Every citation in the given reports, checked; nothing is written."""
    rows = []
    for path in paths:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            covered = []
            def add(kind, m, state, note=None):
                covered.append(m.span())
                rows.append({"report": str(path), "line": number, "citation": m.group(0), "kind": kind,
                             "state": state, **({"note": note} if note else {})})
            for m in TOKEN.finditer(line):
                try:
                    resolve(m.group(0), max_record)
                    add("cite", m, "ok")
                except (OSError, ValueError) as error:
                    add("cite", m, "unresolved", str(error)[:200])
            for m in LONG_HEX.finditer(line):
                covered.append(m.span())  # sha256 and other digests are not citations
            for m in FULL_SHA.finditer(line):
                if not any(a <= m.start() < b for a, b in covered):
                    add("commit", m, _commit(repo, m.group(0)))
            for m in SESSION.finditer(line):
                if not any(a <= m.start() < b for a, b in covered):
                    add("session", m, "not_a_ref", "a session id locates a recording; cite the expanded event's token")
            for m in BARE_HEX.finditer(line):
                if not any(a <= m.start() < b for a, b in covered):
                    add("bare_hex", m, "ambiguous", "a shortened commit, session or hash; cite the full SHA or a cite token")
    problems = [r for r in rows if r["state"] != "ok"]
    return rows, problems
