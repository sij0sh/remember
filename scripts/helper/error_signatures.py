"""Error signatures of one text block (D128): exact line plus a normalized family.

`exact` is the whitespace-collapsed error line (200 chars). `family` replaces volatile parts
(hex addresses, line:col positions, UUIDs, ISO timestamps, temp dirs) so the same failure in
another run, line or temp dir matches; error type, module names, quoted names and codes stay.
The normalization is versioned with the skill's VERSION.
"""
import re

LIMIT = 3
WIDTH = 200
_LINE = re.compile(r"\b\w+(?:Error|Exception)\b|^\s*(?:error|fatal|FAILED)\b|\b(?:E\d{3,}|TS\d{4})\b", re.I | re.M)
_TMP = re.compile(r"(?:/private)?(?:/tmp|/var/folders)/\S*")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")
_HEX = re.compile(r"\b0x[0-9a-f]+\b", re.I)
_POS = re.compile(r":\d+(?::\d+)?\b")
_LINENO = re.compile(r"\bline \d+\b")


def family(exact):
    text = _TMP.sub("<tmp>", exact)
    text = _UUID.sub("<id>", text)
    text = _TIME.sub("<n>", text)
    text = _HEX.sub("<n>", text)
    text = _LINENO.sub("line <n>", text)
    return _POS.sub(":<n>", text)


def _lines(text):
    lines = text.splitlines()
    # A traceback's final line names the exception; earlier frames are positions, not the error.
    if any(line.startswith("Traceback") for line in lines):
        tail = next((line for line in reversed(lines) if line.strip() and not line.startswith(" ")), None)
        if tail:
            yield tail
    for line in lines:
        if _LINE.search(line):
            yield line


def signature(text):
    """[(exact, family)] for up to three distinct error lines, in text order."""
    out, seen = [], set()
    for line in _lines(str(text or "")):
        exact = " ".join(line.split())[:WIDTH]
        if exact and exact not in seen:
            seen.add(exact)
            out.append((exact, family(exact)))
            if len(out) == LIMIT:
                break
    return out
