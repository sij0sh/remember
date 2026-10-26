"""Profile loading and interpretation: pointers, conditions, events, detection."""
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
