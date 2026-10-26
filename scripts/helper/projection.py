"""One row schema and one projection path for display and SQL."""
import json
import shlex
import sys
from pathlib import Path

from .envelope import clip
from .profiles import pointer
from .records import RecordCache

# Every row has this schema, whatever the harness; fields a harness did not record are absent.
FIELDS = ("session", "parent_session", "independent_session", "harness", "kind", "role", "turn", "turn_basis", "timestamp", "tool", "call_id",
          "operation", "path", "path_raw", "path_location", "command", "outcome", "exit_code",
          "representation", "reason", "text", "text_chars", "text_start", "text_omitted", "ref",
          "payload_pointer", "call_ref", "result_ref", "external_turn_id", "external_message_id",
          "external_parent_id", "text_state", "match_at", "text_hash", "event_id", "recording_id",
          "native_type", "native_subtype", "origin", "channel", "purpose", "state", "profile_version",
          "target_id", "replacement", "first_kept_id", "is_shell")


def human(e):
    """A person's request: original user text, not a subagent's or fork's prompt from its parent."""
    return e["kind"] == "user_message" and e.get("origin", "human") == "human" and not e.get("parent_session")


def accepted(e, args):
    return ((not args.text or e.get("_matched")) and (not args.role or args.role == e["role"])
            and (not getattr(args, "human", False) or human(e))
            and (not args.kind or e["kind"] in args.kind)
            and (not args.path or (e.get("path_location") != "unresolved" and str(e.get("path", "")).startswith(args.path))))


def clean(e, args=None, complete=False):
    """Project an event onto the shared row schema; clip only at this display boundary."""
    row = {k: e[k] for k in FIELDS if e.get(k) is not None}
    row.update((k, v) for k, v in e.items() if k.endswith("_omitted_chars"))
    if not complete and row.get("independent_session") == row.get("session"):
        # Displayed rows name the lineage only for a subagent or fork; SQL rows always carry it.
        row.pop("independent_session", None)
    if not complete and args is not None and e.get("text") and len(e["text"]) > args.preview_chars:
        row.update(clip(e["text"], args.preview_chars, match_at=e.get("match_at")))
        if row.get("text_state") == "full":
            row["text_state"] = "previewed"
    return row


# Overview rows orient; ids, hashes, pointers and exchange refs stay one context or select call away.
BRIEF_OMITS = ("payload_pointer", "call_id", "call_ref", "result_ref", "external_turn_id", "external_message_id",
               "external_parent_id", "text_hash", "event_id", "recording_id", "native_type", "native_subtype",
               "channel", "profile_version")


def brief(e, args):
    row = clean(e, args)
    for key in BRIEF_OMITS:
        row.pop(key, None)
    for key in ("text_start", "text_omitted"):
        if row.get(key) == 0:
            del row[key]
    return row


def command(action, ref, **kwargs):
    sessions = Path(__file__).resolve().parents[1] / "sessions.py"
    argv = [sys.executable, str(sessions), action, "--ref", json.dumps(ref, separators=(",", ":"))]
    for k, v in kwargs.items():
        argv.extend(["--" + k.replace("_", "-"), str(v)])
    return shlex.join(argv)


def next_for(e):
    ref = e["ref"]
    items = [{"purpose": "Read this recorded turn", "argv": command("context", ref)}]
    if e["kind"] in ("tool_call", "tool_result"):
        items.append({"purpose": "Read paired exchange", "argv": command("context", ref, mode="exchange")})
    if e.get("payload_pointer"):
        items.append({"purpose": "Read a bounded payload span", "argv": command("payload", ref, pointer=e["payload_pointer"])})
    if e["kind"] in ("gap", "unknown"):
        items.append({"purpose": "Inspect raw bytes", "argv": command("raw", ref)})
    return items


def project_row(e, args, row=None, complete=False, cache=None):
    if row is None:
        row = {k: e.get(k) for k in args.fields.split(",")}
    # Always retain identity and provenance despite arbitrary projections.
    row["harness"] = e["harness"]
    row["ref"] = e["ref"]
    cache = cache or RecordCache(args.max_record_bytes)
    for definition in args.field:
        name, sep, ptr = definition.partition("=")
        if not sep or not name or not ptr.startswith("/") or name in ("ref", "harness"):
            raise ValueError("Use --field alias=/json/pointer; ref and harness are reserved")
        value = pointer(cache.get(e["ref"]), ptr)
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=True)
        if not complete and isinstance(value, str) and len(value) > args.preview_chars:
            row[name] = value[:args.preview_chars]
            row[name + "_omitted_chars"] = len(value) - args.preview_chars
        else:
            row[name] = value
    return row
