"""Profile loading and interpretation: pointers, conditions, events, detection."""
import datetime
import json
import ntpath
import posixpath
import re
from pathlib import Path

PROFILE_DIR = Path(__file__).resolve().parents[1] / "profiles"


def load_profiles():
    """Harness name -> profile; adding a profile file adds a harness."""
    profiles = {}
    for path in sorted(PROFILE_DIR.glob("*.json")):
        profile = json.loads(path.read_text(encoding="utf-8"))
        profiles.setdefault(profile["harness"], profile)
    return profiles


PROFILES = load_profiles()


def pointer(value, path, default=None):
    if path in ("", None):
        return value
    try:
        for part in path.lstrip("/").split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            value = value[int(part)] if isinstance(value, list) else value[part]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        return default


def field(spec, item, key=None):
    if spec is None:
        return None
    if spec == "$key":
        return key
    spec = {"pointer": spec} if isinstance(spec, str) else spec
    value = pointer(item, spec.get("pointer", ""))
    if spec.get("decode") == "json" and isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return None
    if isinstance(value, str) and value.startswith(spec.get("strip_prefix", "\x00")):
        value = value[len(spec["strip_prefix"]):]
    if spec.get("array") == "last" and isinstance(value, list):
        return value[-1] if value else None
    if spec.get("join") == "text_blocks":
        return content_text(value)
    return value


def first(specs, value):
    return next((v for s in specs if (v := field(s, value)) is not None), None)


def stamp(specs, value):
    """The recorded time as text: epoch milliseconds become ISO 8601 UTC, strings stay."""
    found = first(specs, value)
    if isinstance(found, (int, float)) and not isinstance(found, bool):
        try:
            moment = datetime.datetime.fromtimestamp(found / 1000, datetime.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return moment.isoformat().replace("+00:00", "Z")
    return found


def matches(conditions, item, record):
    def equal(left, right):
        if isinstance(left, bool) or isinstance(right, bool):
            return type(left) is type(right) and left == right
        return left == right
    for c in conditions:
        missing = object()
        value = pointer(record if c.get("scope") == "record" else item, c["pointer"], missing)
        if "exists" in c and (value is not missing) != c["exists"]:
            return False
        if "equals" in c and not equal(value, c["equals"]):
            return False
        if "not_equals" in c and equal(value, c["not_equals"]):
            return False
        if "in" in c and not any(equal(value, x) for x in c["in"]):
            return False
        if "type" in c:
            kind = "null" if value is None else "boolean" if isinstance(value, bool) else (
                "string" if isinstance(value, str) else "array" if isinstance(value, list) else
                "object" if isinstance(value, dict) else "number" if isinstance(value, (int, float)) else "missing")
            if kind != c["type"]:
                return False
    return True


def interpret(rules, item, record, path="", key=None):
    for rule in rules:
        if not matches(rule.get("when", []), item, record):
            continue
        if rule["emit"] == "multi":
            for sub in rule.get("events", []):
                if matches(sub.get("when", []), item, record):
                    yield from interpret([sub], item, record, path, key)
        elif rule["emit"] == "each":
            location = rule.get("each", rule.get("each_key"))
            children = pointer(item, location)
            pairs = enumerate(children) if isinstance(children, list) else (
                children.items() if isinstance(children, dict) else [])
            for k, child in pairs:
                escaped = str(k).replace("~", "~0").replace("/", "~1")
                inherited = {name: rule[name] for name in ("representation",) if name in rule}
                children_rules = [{**inherited, **r} for r in rule["rules"]]
                yield from interpret(children_rules, child, record, path + location + "/" + escaped, k)
        else:
            yield rule, item, key, path
        return
    yield {"emit": "unknown"}, item, key, path


def emitted_kinds(rules):
    """Every event kind a rule list can emit, including kinds nested under each/multi rules."""
    kinds = set()
    for rule in rules:
        emit = rule.get("emit")
        if emit == "multi":
            kinds |= emitted_kinds(rule.get("events", []))
        elif emit == "each":
            kinds |= emitted_kinds(rule.get("rules", []))
        elif emit not in (None, "ignore", "unknown"):
            kinds.add(emit)
    return kinds


def detected(value):
    for harness, profile in PROFILES.items():
        if any(matches(conditions, value, value) for conditions in profile.get("detect", [])):
            return harness
    return None


def recorded_root(value, harness=None):
    profile = PROFILES.get(harness or detected(value))
    root = first(profile["session"].get("cwd", []), value) if profile else None
    return root if isinstance(root, str) and root else None


def windows_path(value):
    """True for a path written in native Windows form: drive letter or UNC share."""
    return bool(re.match(r"^[A-Za-z]:[\\/]", value or "") or (value or "").startswith("\\\\"))


def resolve_path(raw, cwd, root):
    """(path, location): a drive or UNC base counts even when the raw path is relative."""
    if not isinstance(raw, str):
        return None, "unresolved"
    pathmod = ntpath if windows_path(raw) or windows_path(cwd) else posixpath
    if not pathmod.isabs(raw) and cwd:
        raw = pathmod.join(cwd, raw)
    if not pathmod.isabs(raw):
        return pathmod.normpath(raw), "unresolved"
    raw = pathmod.normpath(raw)
    if root:
        try:
            if pathmod.commonpath([root, raw]) == pathmod.normpath(root):
                return pathmod.relpath(raw, root).replace("\\", "/"), "repository"
        except ValueError:
            pass
    return raw.replace("\\", "/"), "external"


def content_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(x.get("text", "")) for x in value if isinstance(x, dict) and x.get("type") in ("text", "input_text", "output_text"))
    return ""


# Per-harness read adapters (D145): recorded read result -> observed text and its extent.
CAT_N = re.compile(r"^ *(\d+)(?:\t|→)(.*)$")
PI_MORE = re.compile(r"\n*\[(\d+) more lines in file\. Use offset=(\d+) to continue\.\]\s*$")
DSH_LINE = re.compile(r"^(\d+): ?(.*)$")
DSH_END = re.compile(r"\n*\(End of file - total (\d+) lines\)\s*$")
DSH_RANGE = re.compile(r"\n*\(Showing lines (\d+)-(\d+) of (\d+)\. Use offset=(\d+) to continue\.\)\s*$")


def _line_count(text):
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def read_view(harness, record, text, arguments):
    """{content, start_line, line_count, total_lines, completeness, adapter} or None.

    ``complete`` needs positive evidence: recorded start and totals (Claude), or no
    offset/limit and no truncation marker (pi). Marker: ``truncated``; range: ``range``.
    """
    arguments = arguments if isinstance(arguments, dict) else {}
    ranged = _int(arguments.get("offset")) is not None or _int(arguments.get("limit")) is not None
    if harness == "claude-code":
        result = record.get("toolUseResult") if isinstance(record, dict) else None
        file = result.get("file") if isinstance(result, dict) else None
        if isinstance(file, dict) and isinstance(file.get("content"), str):
            start, count, total = (_int(file.get(k)) for k in ("startLine", "numLines", "totalLines"))
            complete = start == 1 and count is not None and count == total
            return {"content": file["content"], "start_line": start, "line_count": count, "total_lines": total,
                    "completeness": "complete" if complete else "range" if start else "unknown",
                    "adapter": "claude:read_file"}
        if not isinstance(text, str):
            return None
        lines = text.split("\n")
        while lines and not lines[-1]:
            lines.pop()
        matched = [CAT_N.match(line) for line in lines]
        if not lines or not all(matched):
            return None
        return {"content": "\n".join(m.group(2) for m in matched), "start_line": int(matched[0].group(1)),
                "line_count": len(matched), "total_lines": None, "completeness": "unknown",
                "adapter": "claude:cat_n"}
    if harness == "pi" and isinstance(text, str):
        start = _int(arguments.get("offset")) or 1
        more = PI_MORE.search(text)
        if more:
            following = int(more.group(2))
            return {"content": text[:more.start()], "start_line": start, "line_count": following - start,
                    "total_lines": following - 1 + int(more.group(1)), "completeness": "truncated",
                    "adapter": "pi:read_text"}
        # No offset/limit arguments and no truncation marker is the positive evidence (D143).
        count = _line_count(text)
        return {"content": text, "start_line": start, "line_count": count,
                "total_lines": None if ranged else count, "completeness": "range" if ranged else "complete",
                "adapter": "pi:read_text"}
    if harness == "dsh" and isinstance(text, str):
        body = text
        marker = body.find("<content>\n")
        if marker >= 0:
            body = body[marker + len("<content>\n"):]
        if body.endswith("</content>"):
            body = body[:-len("</content>")]
        total = None
        ended = truncated = False
        found = DSH_RANGE.search(body)
        if found:
            total, truncated = int(found.group(3)), True
            body = body[:found.start()]
        else:
            found = DSH_END.search(body)
            if found:
                total, ended = int(found.group(1)), True
                body = body[:found.start()]
        lines = body.split("\n")
        if lines and not lines[-1]:
            lines.pop()
        matched = [DSH_LINE.match(line) for line in lines]
        if not lines or not all(matched):
            return None
        start = int(matched[0].group(1))
        complete = ended and start == 1
        return {"content": "\n".join(m.group(2) for m in matched), "start_line": start,
                "line_count": len(matched), "total_lines": total,
                "completeness": "complete" if complete else "truncated" if truncated
                else "range" if ranged and start else "unknown",
                "adapter": "dsh:read_file"}
    return None
