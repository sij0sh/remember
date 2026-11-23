"""Bounded JSONL reading, raw refs, and record caches. No filesystem writes."""
from .budget import check
from . import zstd
import hashlib
import json
from pathlib import Path


def new_scope():
    return {"files_scanned": 0, "bytes_scanned": 0, "records_scanned": 0,
            "oversized_records": 0, "malformed_records": 0, "unknown_records": 0,
            "ignored_items": 0, "duplicates_skipped": 0, "scan_complete": True,
            "unpaired_results": 0, "calls_without_result": 0, "ambiguous_call_ids": 0,
            "ancestry_unknown": 0, "ancestry_cycles": 0, "events_projected": 0, "text_bytes_stored": 0,
            "text_truncated_events": 0, "text_bytes_omitted": 0, "text_complete": True,
            "representation_variants": 0, "recordings_with_parent": 0,
            "records_unrepresented": 0, "items_interpreted": 0, "interpretation_unsupported": 0,
            "interpretation_gaps": 0, "tail_alignment_bytes": 0, "tail_alignment_skipped": 0}


def records(path, scope, max_record, max_scan, ranges=None):
    """Drain giant lines in bounded chunks; preserve byte offsets and hashes.

    ``ranges`` is an optional list of ``(start, end)`` byte ranges. A nonzero
    start snaps forward to the next line boundary. Records from a tail range
    carry ``line=None`` because their line number is not known without reading
    the skipped bytes; their offset and hash remain exact.

    A compressed artifact is read through the decoder as one forward stream:
    offsets are decompressed offsets, the budget counts decompressed bytes, and
    a byte range is refused because the source cannot seek.
    """
    compressed = zstd.compressed(path)
    if compressed and ranges:
        raise ValueError(f"{path}: a compressed recording cannot be scanned by byte range")
    ranges = ranges or [(0, None)]
    if compressed:
        stream = zstd.open_stream(path)
        if stream is None:
            raise ValueError(f"{path}: no zstd decoder; install the zstandard package or the zstd executable")
    else:
        stream = open(path, "rb")
    scope["source_eof"] = False
    with stream:
        for start, end in ranges:
            if start:
                # Start at a complete line, never in the middle of a record.
                # The skipped alignment bytes are charged to the scan budget too;
                # a sampled tail must not bypass --max-scan-bytes through one huge line.
                stream.seek(start - 1)
                if stream.read(1) != b"\n":
                    scope["tail_alignment_skipped"] += 1
                    while True:
                        remaining = max_scan - scope["bytes_scanned"]
                        if remaining <= 0:
                            scope["scan_complete"] = False
                            scope["scan_limit_reached"] = True
                            return
                        chunk = stream.readline(min(65536, remaining))
                        if not chunk:
                            break
                        scope["bytes_scanned"] += len(chunk)
                        scope["tail_alignment_bytes"] += len(chunk)
                        if chunk.endswith(b"\n"):
                            break
                        if len(chunk) >= remaining:
                            scope["scan_complete"] = False
                            scope["scan_limit_reached"] = True
                            return
            line_no = 0
            while True:
                check()
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
                    scope["source_eof"] = True
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
                if compressed:
                    ref["compression"] = "zstd"
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
    error = getattr(stream, "error", None)
    if error:
        # Evidence already yielded stands; the rest of this artifact is not readable.
        scope["scan_complete"] = False
        scope["source_eof"] = False
        scope["decompression_failed"] = f"{Path(path).name}: {error}"


def safe_ref(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or not {"file", "offset", "length", "sha256"} <= value.keys():
        raise ValueError("Expected a returned ref object")
    if not isinstance(value["offset"], int) or value["offset"] < 0 or not isinstance(value["length"], int) or value["length"] < 0:
        raise ValueError("Invalid byte range")
    return value


# Decompressed bytes one RecordCache may hold; larger recordings stream per lookup.
ZSTD_CACHE_BYTES = 64 * 1024 * 1024


def decompressed(path, limit):
    """Whole decompressed recording, or None over `limit`, on a decode error, or with no decoder."""
    stream = zstd.open_stream(path)
    if stream is None:
        return None
    chunks, size = [], 0
    with stream:
        while size <= limit:
            chunk = stream.read(zstd.CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    return None if stream.error or size > limit else b"".join(chunks)


def read_ref(ref, max_record=2 * 1024 * 1024, whole=None):
    """`whole(path)` may return a recording's decompressed bytes to slice instead of streaming."""
    ref = safe_ref(ref)
    if ref["length"] > max_record:
        raise ValueError("Oversized record: use raw or explicitly raise --max-record-bytes")
    data = whole(ref["file"]) if whole and ref.get("compression") == "zstd" else None
    if data is not None:
        raw = data[ref["offset"]:ref["offset"] + ref["length"]]
    elif ref.get("compression") == "zstd":
        stream = zstd.open_stream(ref["file"])
        if stream is None:
            raise ValueError("No zstd decoder; install the zstandard package or the zstd executable")
        with stream:
            stream.read(ref["offset"])
            raw = stream.read(ref["length"])
        if stream.error:
            raise ValueError(stream.error)
    else:
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
        self._zstd = {}
        self._zstd_left = ZSTD_CACHE_BYTES

    def _whole(self, path):
        """Decompress each zstd recording once per response, within the byte cap."""
        if path not in self._zstd:
            data = decompressed(path, self._zstd_left)
            self._zstd[path] = data
            if data is not None:
                self._zstd_left -= len(data)
        return self._zstd[path]

    def get(self, ref):
        key = (ref["file"], ref["offset"], ref["length"], ref["sha256"])
        if key not in self._records:
            self._records[key] = read_ref(ref, self.max_record, self._whole)
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
