"""Bounded JSONL reading, raw refs, and record caches. No filesystem writes."""
import hashlib
import json
from pathlib import Path


def new_scope():
    return {"files_scanned": 0, "bytes_scanned": 0, "records_scanned": 0,
            "oversized_records": 0, "malformed_records": 0, "unknown_records": 0,
            "ignored_items": 0, "duplicates_skipped": 0, "scan_complete": True,
            "unpaired_results": 0, "calls_without_result": 0, "ambiguous_call_ids": 0,
            "ancestry_unknown": 0, "events_projected": 0, "text_bytes_stored": 0,
            "text_truncated_events": 0, "text_bytes_omitted": 0, "text_complete": True,
            "representation_variants": 0, "recordings_with_parent": 0}


def records(path, scope, max_record, max_scan, ranges=None):
    """Drain giant lines in bounded chunks; preserve byte offsets and hashes.

    ``ranges`` is an optional list of ``(start, end)`` byte ranges. A nonzero
    start snaps forward to the next line boundary. Records from a tail range
    carry ``line=None`` because their line number is not known without reading
    the skipped bytes; their offset and hash remain exact.
    """
    ranges = ranges or [(0, None)]
    with open(path, "rb") as stream:
        for start, end in ranges:
            if start:
                # Start at a complete line, never in the middle of a record.
                stream.seek(start - 1)
                if stream.read(1) != b"\n":
                    stream.readline()
            line_no = 0
            while True:
                offset = stream.tell()
                if end is not None and offset >= end:
                    break
                remaining = max_scan - scope["bytes_scanned"]
                if remaining <= 0:
                    scope["scan_complete"] = False
                    scope["scan_limit_reached"] = True
                    return
                limit = min(max_record + 1, remaining)
                if end is not None:
                    limit = min(limit, end - offset)
                if limit <= 0:
                    break
                line = stream.readline(limit)
                if not line:
                    break
                line_no += 1
                digest = hashlib.sha256(line)
                length = len(line)
                scope["bytes_scanned"] += length
                oversized = length > max_record
                terminated = line.endswith(b"\n")
                if not terminated and scope["bytes_scanned"] >= max_scan and stream.peek(1):
                    scope["scan_complete"] = False
                    scope["scan_limit_reached"] = True
                    return
                while not terminated and len(line) and length >= max_record + 1:
                    remaining = max_scan - scope["bytes_scanned"]
                    if remaining <= 0:
                        scope["scan_complete"] = False
                        scope["scan_limit_reached"] = True
                        return
                    take = min(65536, remaining)
                    if end is not None:
                        take = min(take, end - stream.tell())
                        if take <= 0:
                            break
                    chunk = stream.readline(take)
                    if not chunk:
                        break
                    digest.update(chunk)
                    length += len(chunk)
                    scope["bytes_scanned"] += len(chunk)
                    terminated = chunk.endswith(b"\n")
                # A head range may end mid-record: that partial record is not evidence.
                if end is not None and not terminated and stream.tell() >= end:
                    break
                ref = {"file": str(Path(path).absolute()),
                       "line": (line_no if start == 0 else None), "offset": offset,
                       "length": length, "sha256": digest.hexdigest()}
                scope["records_scanned"] += 1
                if oversized:
                    scope["oversized_records"] += 1
                    yield ref, None, "oversized_record"
                    continue
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("record is not an object")
                    yield ref, value, None
                except (ValueError, UnicodeDecodeError, RecursionError):
                    scope["malformed_records"] += 1
                    yield ref, None, "malformed_record"


def safe_ref(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or not {"file", "offset", "length", "sha256"} <= value.keys():
        raise ValueError("Expected a returned ref object")
    if not isinstance(value["offset"], int) or value["offset"] < 0 or not isinstance(value["length"], int) or value["length"] < 0:
        raise ValueError("Invalid byte range")
    return value


def read_ref(ref, max_record=2 * 1024 * 1024):
    ref = safe_ref(ref)
    if ref["length"] > max_record:
        raise ValueError("Oversized record: use raw or explicitly raise --max-record-bytes")
    with open(ref["file"], "rb") as stream:
        stream.seek(ref["offset"])
        raw = stream.read(ref["length"])
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise ValueError("Source changed at this ref; search again")
    return json.loads(raw)


class RecordCache:
    """Decode each source record once per response, independently of row count."""

    def __init__(self, max_record=2 * 1024 * 1024):
        self.max_record = max_record
        self._records = {}

    def get(self, ref):
        key = (ref["file"], ref["offset"], ref["length"], ref["sha256"])
        if key not in self._records:
            self._records[key] = read_ref(ref, self.max_record)
        return self._records[key]

    def payload(self, event):
        from .git_commands import value_text
        from .profiles import pointer
        if not event:
            return ""
        ptr = event.get("payload_pointer")
        if not ptr:
            return event.get("text") or ""
        try:
            return value_text(pointer(self.get(event["ref"]), ptr))
        except (KeyError, IndexError, TypeError, ValueError):
            return event.get("text") or ""
