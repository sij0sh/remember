"""One bounded JSON envelope, coverage summary, and display clipping."""
import json
import sys

SCHEMA_VERSION = 1


def clip(text, chars=400, needle=None, tail=False, match_at=None):
    text = str(text)
    if tail:
        start = max(0, len(text) - chars)
    elif match_at is not None and match_at >= 0:
        start = max(0, match_at - chars // 3)
    elif needle and needle.casefold() in text.casefold():
        import re
        m = re.search(re.escape(needle), text, re.IGNORECASE)
        start = max(0, (m.start() if m else 0) - chars // 3)
    else:
        start = 0
    shown = text[start:start + chars]
    return {"text": shown, "text_chars": len(text), "text_start": start,
            "text_omitted": len(text) - len(shown)}


def envelope(rows, scope=None, *, grain=None, omissions=None, next=None, status=None, **extra):
    value = {"schema_version": SCHEMA_VERSION, "results": rows}
    if scope is not None:
        value["scope"] = scope
    if grain is not None:
        value["grain"] = grain
    value["omissions"] = omissions or {}
    if next is not None:
        value["next"] = next
    if status is not None:
        value["status"] = status
    value.update(extra)
    return value


def omitted_rows(omissions):
    """Rows an envelope dropped by paging or budget: these make aggregates undercount."""
    if not isinstance(omissions, dict):
        return 0
    return sum(v for k, v in omissions.items() if isinstance(v, int) and not isinstance(v, bool)
               and k in ("selection", "results_budget", "rows_budget", "links_budget", "upstream_rows"))


def incomplete(value, scope):
    """Reasons an envelope reports it is known to be incomplete; [] means none reported, not complete."""
    reasons = []
    if scope.get("scan_complete") is False:
        reasons.append("scan_limit")
    if scope.get("discovery_complete") is False:
        reasons.append("discovery_limit")
    if scope.get("project_unknown"):
        reasons.append("project_unknown")
    repository = scope.get("repository") if isinstance(scope.get("repository"), dict) else {}
    if any(isinstance(part, dict) and part.get("complete") is False for part in repository.values()):
        reasons.append("repository")
    omissions = value.get("omissions") if isinstance(value.get("omissions"), dict) else {}
    if omissions.get("more_query_rows") is True:
        reasons.append("query_limit")
    if omissions.get("envelope_omitted"):
        reasons.append("output_budget")
    if value.get("status") == "partial":
        reasons.append("upstream_partial")
    return reasons


def coverage(value):
    """A small summary of an input envelope's coverage, not a copy of its scope."""
    scope = value.get("scope", {}) if isinstance(value.get("scope"), dict) else {}
    groups = value.get("harnesses") if isinstance(value.get("harnesses"), list) else []
    omissions = {k: v for k, v in value.get("omissions", {}).items() if v} if isinstance(value.get("omissions"), dict) else {}
    for g in groups:
        for k, v in (g.get("omissions", {}) if isinstance(g, dict) else {}).items():
            if v:
                omissions[f"{g.get('harness')}.{k}"] = v
    dropped = omitted_rows(value.get("omissions", {})) + sum(omitted_rows(g.get("omissions", {})) for g in groups)
    summary = {"scope": {k: scope[k] for k in ("files_scanned", "scan_complete", "discovery_complete",
                                              "project_filter", "harnesses", "text_complete",
                                              "text_truncated_events") if k in scope},
               "matched": value.get("matched"), "rows_omitted": dropped, "omissions": omissions}
    if value.get("status"):
        summary["status"] = value["status"]
    reasons = incomplete(value, scope)
    if reasons:
        # Known incompleteness only: an envelope that reports nothing stays unknown, not complete.
        summary["complete"] = False
        summary["incomplete_reasons"] = reasons
    if scope.get("text_complete") is False:
        summary["status"] = "partial"
        summary["text_truncated_events"] = scope.get("text_truncated_events", 0)
        summary["omissions"]["text_truncated_events"] = summary["text_truncated_events"]
    return summary


def load_rows(raw):
    """Parse an envelope, array or JSONL into rows; unknown envelopes are errors."""
    try:
        value = json.loads(raw)
    except ValueError:
        try:
            return [json.loads(line) for line in raw.splitlines() if line.strip()], {}
        except ValueError:
            raise ValueError("Input is not JSON, a JSON envelope, or JSONL object rows") from None
    if isinstance(value, dict):
        version = value.get("schema_version")
        if isinstance(version, int) and version > SCHEMA_VERSION:
            raise ValueError(f"Envelope schema_version {version} is newer than supported {SCHEMA_VERSION}")
        omissions = value.get("omissions") if isinstance(value.get("omissions"), dict) else {}
        if omissions.get("envelope_omitted"):
            # A coverage-only envelope lost its rows to the byte budget: unavailable, never zero rows.
            more = value.get("detail", {}).get("more_bytes") if isinstance(value.get("detail"), dict) else None
            raise ValueError("Input envelope omitted its rows to fit --max-bytes; rerun the producer with "
                             + (f"--max-bytes {more} or more" if more else "a larger --max-bytes"))
        if isinstance(value.get("harnesses"), list):
            # Grouped sessions.py envelopes: one table of all harnesses' rows, each row tagged.
            rows = []
            for group in value["harnesses"]:
                if not isinstance(group, dict):
                    raise ValueError("harnesses[] entries must be objects")
                body = group.get("results", group.get("rows", group.get("links")))
                if not isinstance(body, list):
                    raise ValueError("Every harness group must hold results[], rows[], or links[]")
                rows.extend(body)
            return rows, coverage(value)
        body = value.get("results", value.get("rows", value.get("links")))
        if not isinstance(body, list):
            raise ValueError("Expected a JSON envelope with results[] or rows[] (or links[]), an array, or JSONL object rows")
        return body, coverage(value)
    if not isinstance(value, list):
        raise ValueError("Expected a JSON envelope, array, or JSONL object rows")
    return value, {}


# Sections shed before the rows a command was asked for: ref-bearing examples first, then
# ref-free aggregates, which cost little per entry.
EXAMPLES = ("leads", "recordings", "next")
AGGREGATES = ("files", "commands")
OPTIONAL = EXAMPLES + AGGREGATES
PRIMARY = ("requests", "results", "rows", "links")
# Coverage that survives every level; strings are clipped so the floor fits the minimum budget.
FLOOR_SCOPE = ("files_scanned", "scan_complete", "discovery_complete", "text_complete", "events_projected",
               "project_filter", "sessions_independent", "project_unknown", "scan_limit_reached",
               "event_limit_reached")
MIN_BYTES = 1024


def _holders(value):
    return [value] + [g for g in value.get("harnesses", []) if isinstance(g, dict)]


def _partial(value):
    """Primary rows left the output; an error status says more and is kept."""
    if value.get("status") != "error":
        value["status"] = "partial"


def _drop(holder, key, n):
    del holder[key][len(holder[key]) - n:]
    omissions = holder.setdefault("omissions", {})
    omissions[key + "_budget"] = omissions.get(key + "_budget", 0) + n
    if key == "requests" and isinstance(holder.get("activity"), dict):
        holder["activity"]["requests_shown"] = len(holder["requests"])


def _sparse(value, budget):
    """Lossless: zero counters are omitted, so an absent counter means 0."""
    for holder in _holders(value):
        scope = holder.get("scope")
        if isinstance(scope, dict):
            for key in [k for k, v in scope.items() if type(v) is int and v == 0 and k != "files_scanned"]:
                del scope[key]


def _snapshot(value, budget):
    """Refs carry offsets and hashes, so the per-file stat snapshot is the first lossy cut."""
    scope = value.get("scope")
    if isinstance(scope, dict) and isinstance(scope.get("snapshot"), list):
        value["omissions"]["snapshot_files"] = len(scope.pop("snapshot"))
        scope.pop("snapshot_basis", None)


def _previews(chars):
    def shrink(value, budget):
        for holder in _holders(value):
            for key in OPTIONAL + PRIMARY:
                for row in holder.get(key) if isinstance(holder.get(key), list) else ():
                    text = row.get("text") if isinstance(row, dict) else None
                    if isinstance(text, str) and len(text) > chars:
                        row["text"] = text[:chars]
                        row["text_omitted"] = row.get("text_omitted", 0) + len(text) - chars
                        if row.get("text_state") == "full":
                            row["text_state"] = "previewed"
                    command = row.get("command") if isinstance(row, dict) else None
                    if isinstance(command, str) and len(command) > chars:
                        row["command"] = command[:chars]
                        row["command_omitted_chars"] = len(command) - chars
    return shrink


def _examples(keep, keys=OPTIONAL):
    def shrink(value, budget):
        for holder in _holders(value):
            for key in keys:
                items = holder.get(key)
                if not isinstance(items, list):
                    continue
                # Coverage reruns lead next and are never shed: they are how a partial scan recovers.
                floor = keep
                while key == "next" and floor < len(items) and isinstance(items[floor], dict) \
                        and items[floor].get("coverage"):
                    floor += 1
                if len(items) > floor:
                    _drop(holder, key, len(items) - floor)
    return shrink


def _group_scope(value, budget):
    """Envelope scope keeps the totals; groups keep only what locates each harness."""
    for holder in _holders(value)[1:]:
        if isinstance(holder.get("scope"), dict):
            holder["scope"] = {k: v for k, v in holder["scope"].items() if k in FLOOR_SCOPE}


def _rows(value, budget):
    """Drop primary rows from the end of the longest list, so ranking and --offset paging hold."""
    lists = [(h, k) for h in _holders(value) for k in PRIMARY if isinstance(h.get(k), list)]
    size = len(encoded(value))
    while size > budget:
        live = [c for c in lists if c[0][c[1]]]
        if not live:
            return
        holder, key = max(live, key=lambda c: len(c[0][c[1]]))
        # A dropped row takes its bytes and one comma; re-encode once the estimate fits.
        size -= len(encoded(holder[key][-1]))
        _drop(holder, key, 1)
        _partial(value)
        if size <= budget:
            size = len(encoded(value))


def _floor(value, budget):
    """Coverage only, bounded by construction below MIN_BYTES; coverage reruns are kept while they fit."""
    scope = value.get("scope") if isinstance(value.get("scope"), dict) else {}
    reruns = [n for n in value.get("next", []) if isinstance(n, dict) and n.get("coverage")]
    floor = {"schema_version": SCHEMA_VERSION,
             "scope": {k: (scope[k][:160] if isinstance(scope[k], str) else scope[k])
                       for k in FLOOR_SCOPE if isinstance(scope.get(k), (bool, int, str))},
             "omissions": {"envelope_omitted": True}}
    if isinstance(value.get("detail"), dict):
        floor["detail"] = value["detail"]
    floor["status"] = "error" if value.get("status") == "error" else "partial"
    if isinstance(value.get("matched"), int):
        floor["matched"] = value["matched"]
    groups = [g for g in value.get("harnesses", []) if isinstance(g, dict)][:4]
    if groups:
        floor["harnesses"] = [{"harness": str(g.get("harness"))[:24],
                               **{k: g["scope"][k] for k in ("files_scanned", "events_projected")
                                  if isinstance(g.get("scope"), dict) and type(g["scope"].get(k)) is int},
                               **({"request_count": g["activity"]["request_count"]}
                                  if isinstance(g.get("activity"), dict) and type(g["activity"].get("request_count")) is int else {})}
                              for g in groups]
    for item in reruns:
        floor.setdefault("next", []).append(item)
        if len(encoded(floor)) > budget:
            floor["next"].pop()
            floor["omissions"]["next_budget"] = floor["omissions"].get("next_budget", 0) + 1
    if not floor.get("next"):
        floor.pop("next", None)
    value.clear()
    value.update(floor)


# Coarser renderings of the same envelope, lossless first. Each level applies on top of the
# previous ones; the first level whose encoding fits is emitted, so output is a pure function of
# the result and the budget.
LEVELS = (("full", None), ("sparse", _sparse), ("no_snapshot", _snapshot), ("previews_160", _previews(160)),
          ("examples_2", _examples(2)), ("group_scope", _group_scope), ("examples_0", _examples(0, EXAMPLES)),
          ("previews_80", _previews(80)), ("aggregates_1", _examples(1, AGGREGATES)),
          ("aggregates_0", _examples(0, AGGREGATES)), ("rows", _rows),
          ("coverage", _floor))


def _delivery(result):
    """complete is true only when no limit, paging, budget or text cut is reported anywhere in the envelope."""
    scope = result.get("scope") if isinstance(result.get("scope"), dict) else {}
    reasons = [r for r in incomplete(result, scope) if r != "upstream_partial"]
    if any(omitted_rows(h.get("omissions")) for h in _holders(result)):
        reasons.append("rows_omitted")
    if any(k.endswith("_budget") and v for h in _holders(result) for k, v in (h.get("omissions") or {}).items()):
        reasons.append("output_budget")
    if scope.get("text_complete") is False:
        reasons.append("text_truncated")
    if result.get("status") == "partial" and not reasons:
        reasons.append("partial")
    reasons = list(dict.fromkeys(reasons))
    result["delivery"] = {"complete": not reasons, **({"incomplete_reasons": reasons} if reasons else {})}


def emit(result, max_bytes=8192):
    """Emit the most detailed level that fits; never truncate serialized bytes."""
    if max_bytes < MIN_BYTES:
        raise ValueError(f"--max-bytes must be at least {MIN_BYTES}")
    if isinstance(result, dict):
        result.setdefault("schema_version", SCHEMA_VERSION)
    result.setdefault("omissions", {})
    for holder in _holders(result):
        holder.setdefault("omissions", {})
    richer = None
    for name, shrink in LEVELS:
        if richer is not None:
            # more_bytes is the exact budget that returns the previous, richer level.
            result["detail"] = {"level": name, "more_bytes": richer}
        if shrink is not None:
            shrink(result, max_bytes)
        if isinstance(result, dict) and result.get("status") != "error":
            _delivery(result)
        size = len(encoded(result))
        if size <= max_bytes:
            break
        richer = size
    sys.stdout.buffer.write(encoded(result))


def encoded(value):
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
