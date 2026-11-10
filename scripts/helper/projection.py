"""One row schema and one projection path for display and SQL."""
import json
import sys
from pathlib import Path

from .envelope import clip
from .options import command_line

# Every row has this schema, whatever the harness; fields a harness did not record are absent.
FIELDS = ("session", "parent_session", "independent_session", "harness", "kind", "role", "turn", "turn_basis",
          "request_ref", "request_basis", "request_origin", "lane", "lane_basis", "branch", "branch_basis",
          "message_id", "ancestry_status", "ancestry_depth", "message_ambiguous", "seq", "position",
          "byte_offset", "item_index", "timestamp", "tool", "call_id",
          "operation", "path", "path_raw", "path_location", "command", "outcome", "exit_code",
          "representation", "reason", "text", "text_chars", "text_start", "text_omitted", "ref",
          "payload_pointer", "call_ref", "result_ref", "external_turn_id", "external_message_id",
          "external_parent_id", "text_state", "match_at", "text_hash", "event_id", "recording_id",
          "native_type", "native_subtype", "origin", "channel", "purpose", "state", "profile_version",
          "target_id", "replacement", "first_kept_id", "is_shell")


def human(e):
    """A person's request: original user text, not injected context or a delegated parent prompt."""
    if e.get("request_origin") is not None:
        return (e["kind"] == "user_message" and e["request_origin"] == "human"
                and e.get("origin", "human") == "human")
    return e["kind"] == "user_message" and e.get("origin", "human") == "human" and not e.get("parent_session")


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


def next_for(e):
    """Follow-up for a recorded event: its exchange, through the common show operation."""
    remember = Path(__file__).resolve().parents[1] / "remember.py"
    # The JSON ref resolves anywhere; a cite token only under configured recording roots.
    return [{"purpose": "Read this recorded exchange and its turn's request",
             "argv": command_line([sys.executable, str(remember), "show", json.dumps(e["ref"], separators=(",", ":"))])}]


