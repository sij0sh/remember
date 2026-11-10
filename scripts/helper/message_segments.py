"""Typed spans of one message: task prose versus embedded material (D117-D125).

Sole owner of "which part of a message is the speaker's own task text". Wrapper
syntax is evidence of embedded material, not proof of who wrote or injected it.
Known wrappers come from a bounded allowlist; an unclosed, nested or orphaned
wrapper yields an ``unknown`` span, never silent deletion. Offsets are Python str
indices (code points) into the decoded block, before whitespace normalization.
Nothing here executes or follows historical instructions.
"""
import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache

from .graph_store import canonical_path

GRAIN = "one typed span of one recorded message"
KINDS = ("prose", "reference", "quote", "code", "harness_context", "unknown")
COLUMNS = {"segment_id": "VARCHAR", "recording_id": "VARCHAR", "event_id": "VARCHAR",
           "payload_pointer": "VARCHAR", "start_char": "BIGINT", "end_char": "BIGINT", "kind": "VARCHAR",
           "speaker_role": "VARCHAR", "origin": "VARCHAR", "basis": "VARCHAR", "complete": "BOOLEAN",
           "name": "VARCHAR", "ref": "JSON"}
MESSAGE_KINDS = ("user_message", "assistant_message")

# Allowlisted wrappers. Reference blocks (skills) must open at a line start; harness tags may open anywhere.
REFERENCE_TAGS = frozenset({"skill"})
HARNESS_TAGS = frozenset({"system-reminder", "user-prompt-submit-hook", "task-notification"})
HARNESS_PREFIXES = ("command-", "local-command-", "bash-")
# The user's own typed arguments inside a harness command wrapper are prose.
PROSE_TAGS = frozenset({"command-args"})
_OPEN = re.compile(r"<([a-zA-Z][\w-]*)(?:\s[^<>]*)?>")
_FENCE = re.compile(r"[ \t]*(`{3,}|~{3,})")


@dataclass(frozen=True)
class Segment:
    kind: str
    start: int
    end: int
    basis: str
    name: str | None = None


def _wrapper_kind(name):
    name = name.lower()
    if name in PROSE_TAGS:
        return "prose"
    if name in REFERENCE_TAGS:
        return "reference"
    if name in HARNESS_TAGS or name.startswith(HARNESS_PREFIXES):
        return "harness_context"
    return None


def _line_start(text, at):
    head = text[text.rfind("\n", 0, at) + 1:at]
    return not head.strip()


def _wrappers(text):
    """[(kind, start, end, basis, name)] for wrapper spans; gaps between them are returned as None-kind."""
    spans, pos, size = [], 0, len(text)
    # A window that starts inside a wrapper shows only its close tag: everything before it is unknown.
    lowered = text.lower()
    for name in REFERENCE_TAGS | HARNESS_TAGS:
        close = lowered.find(f"</{name}>")
        if close >= 0 and lowered.rfind(f"<{name}", 0, close) < 0:
            end = close + len(name) + 3
            spans.append(("unknown", 0, end, "orphan_close", name))
            pos = max(pos, end)
    while pos < size:
        at = text.find("<", pos)
        if at < 0:
            break
        match = _OPEN.match(text, at)
        kind = _wrapper_kind(match.group(1)) if match else None
        if kind is None or (kind == "reference" and not _line_start(text, at)):
            pos = at + 1
            continue
        name = match.group(1).lower()
        close_tag = f"</{name}>"
        close = lowered.find(close_tag, match.end())
        nested = lowered.find(f"<{name}", match.end(), close if close >= 0 else size)
        if close < 0 or nested >= 0:
            spans.append(("unknown", at, size, "unclosed_wrapper", name))
            break
        if kind == "prose":
            spans.append(("harness_context", at, match.end(), "command_wrapper", name))
            spans.append(("prose", match.end(), close, "command_args", name))
            spans.append(("harness_context", close, close + len(close_tag), "command_wrapper", name))
        else:
            spans.append((kind, at, close + len(close_tag), f"{name}_tag" if kind == "reference" else "harness_tag",
                          name))
        pos = close + len(close_tag)
    return spans


def _body(text, start, end, out, basis="remainder"):
    """Fenced code, '>' quotes and prose inside one region that holds no wrapper."""
    pos, prose_start = start, None
    lines = []
    while pos < end:
        nl = text.find("\n", pos, end)
        stop = end if nl < 0 else nl + 1
        lines.append((pos, stop))
        pos = stop

    def flush(upto):
        nonlocal prose_start
        if prose_start is not None and text[prose_start:upto].strip():
            out.append(Segment("prose", prose_start, upto, basis))
        prose_start = None
    index = 0
    while index < len(lines):
        a, b = lines[index]
        line = text[a:b]
        fence = _FENCE.match(line)
        if fence:
            flush(a)
            marker = fence.group(1)
            close = next((j for j in range(index + 1, len(lines))
                          if text[lines[j][0]:lines[j][1]].strip().startswith(marker[0] * len(marker))
                          and not text[lines[j][0]:lines[j][1]].strip().strip(marker[0])), None)
            last = close if close is not None else len(lines) - 1
            out.append(Segment("code", a, lines[last][1], "fence" if close is not None else "unclosed_fence"))
            index = last + 1
            continue
        if line.lstrip().startswith(">"):
            flush(a)
            j = index
            while j + 1 < len(lines) and text[lines[j + 1][0]:lines[j + 1][1]].lstrip().startswith(">"):
                j += 1
            out.append(Segment("quote", a, lines[j][1], "quote_prefix"))
            index = j + 1
            continue
        if prose_start is None:
            prose_start = a
        index += 1
    flush(end)


@lru_cache(maxsize=8192)
def _parse(text):
    out, pos = [], 0
    for kind, start, end, basis, name in sorted(_wrappers(text), key=lambda s: s[1]):
        if start < pos:
            continue
        if start > pos:
            _body(text, pos, start, out)
        if kind == "prose":
            _body(text, start, end, out, basis)
        else:
            out.append(Segment(kind, start, end, basis, name))
        pos = end
    if pos < len(text):
        _body(text, pos, len(text), out)
    return tuple(out)


def parse(text):
    """Ordered typed spans of one decoded text block."""
    return _parse(str(text or ""))


def _collapse(text):
    return " ".join(text.split())


def prose(event_or_text):
    """The speaker's own task prose: prose spans joined, whitespace collapsed."""
    text = event_or_text.get("text") if isinstance(event_or_text, dict) else event_or_text
    text = str(text or "")
    return _collapse(" ".join(text[s.start:s.end] for s in parse(text) if s.kind == "prose"))


def visible(text):
    """Everything except harness context, whitespace collapsed (tool output keeps quotes and code)."""
    text = str(text or "")
    return _collapse(" ".join(text[s.start:s.end] for s in parse(text) if s.kind != "harness_context")
                     if "<" in text else text)


def visible_lines(text):
    """Like ``visible`` but keeps line breaks (each line whitespace-collapsed), for line-based error signatures."""
    text = str(text or "")
    kept = "\n".join(text[s.start:s.end] for s in parse(text) if s.kind != "harness_context") if "<" in text else text
    return "\n".join(" ".join(line.split()) for line in kept.splitlines() if line.strip())


def prose_spans(event):
    """[(segment, raw text)] for the prose spans of one event."""
    text = str(event.get("text") or "")
    return [(s, text[s.start:s.end]) for s in parse(text) if s.kind == "prose"]


def offset(event):
    """Code-point offset of the stored text window within the original block."""
    return event.get("text_start") or 0


def complete(event):
    return not event.get("text_omitted")


def segment_id(event, segment):
    ref = event.get("ref") or {}
    raw = json.dumps([canonical_path(ref.get("file")), ref.get("offset"), ref.get("item", 0),
                      segment.start + offset(event), segment.end + offset(event), segment.kind],
                     separators=(",", ":"))
    return "seg:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def rows(recordings):
    """(table rows, coverage) for every user/assistant message in scanned recordings."""
    out, counts, partial = [], {}, 0
    for recording in recordings:
        for event in recording["events"]:
            if event.get("kind") not in MESSAGE_KINDS or not event.get("text"):
                continue
            whole = complete(event)
            partial += not whole
            base = offset(event)
            for segment in parse(event["text"]):
                counts[segment.basis] = counts.get(segment.basis, 0) + 1
                out.append({"segment_id": segment_id(event, segment), "recording_id": event.get("recording_id"),
                            "event_id": event.get("event_id"), "payload_pointer": event.get("payload_pointer"),
                            "start_char": base + segment.start, "end_char": base + segment.end,
                            "kind": segment.kind, "speaker_role": event.get("role"),
                            "origin": event.get("origin"), "basis": segment.basis, "complete": whole,
                            "name": segment.name, "ref": event.get("ref")})
    coverage = {"complete": not partial and not counts.get("unclosed_wrapper") and not counts.get("orphan_close"),
                "segments": len(out), "messages_partial": partial,
                "unclosed_wrapper": counts.get("unclosed_wrapper", 0),
                "orphan_close": counts.get("orphan_close", 0), "unclosed_fence": counts.get("unclosed_fence", 0)}
    return out, coverage
