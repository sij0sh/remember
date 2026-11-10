"""Recorded summary occurrences decoded into attributed claims, not outcomes.

One owned decoder turns Pi Shadow structured checkpoints, Pi rendered/native
compactions and Claude standard summaries into three transient grains: the
physical occurrence (``summaries``), its original item/subfield text
(``summary_items``) and snapshot-scoped citation labels (``summary_refs``).
Nothing here promotes a summary claim to a recorded outcome, user authority or
canonical installation; lifecycle evidence stays a separate dimension.
"""
import json
import re

from .profiles import pointer

PARSER_VERSION = "1"
SUMMARY_GRAIN = "one recorded summary occurrence; join events USING(recording_id)"
ITEM_GRAIN = "one original summary item or continuation subfield; join summaries USING(summary_id)"
REF_GRAIN = "one cited label on one summary item"
SUMMARY_COLUMNS = {
    "summary_id": "VARCHAR", "recording_id": "VARCHAR", "session": "VARCHAR",
    "independent_session": "VARCHAR", "harness": "VARCHAR", "lane": "VARCHAR", "branch": "VARCHAR",
    "order": "BIGINT", "timestamp": "VARCHAR", "event_id": "VARCHAR", "ref": "JSON",
    "physical_kind": "VARCHAR", "purpose": "VARCHAR", "representation": "VARCHAR",
    "producer": "VARCHAR", "producer_basis": "VARCHAR", "producer_id": "VARCHAR",
    "twin_key": "VARCHAR", "twin_status": "VARCHAR",
    "schema_version": "VARCHAR", "parser_version": "VARCHAR",
    "coverage_start_id": "VARCHAR", "coverage_end_id": "VARCHAR", "coverage_status": "VARCHAR",
    "coverage_basis": "VARCHAR", "prepared_leaf_id": "VARCHAR", "published_leaf_id": "VARCHAR",
    "first_kept_id": "VARCHAR", "retained_anchor": "VARCHAR", "anchor_id": "VARCHAR",
    "logical_parent_id": "VARCHAR", "retained_ids": "JSON",
    "compact_trigger": "VARCHAR", "pre_tokens": "BIGINT", "duration_ms": "BIGINT",
    "preserved_head_id": "VARCHAR", "preserved_tail_id": "VARCHAR",
    "citation_availability": "VARCHAR", "snapshot_id": "VARCHAR",
    "acceptance": "VARCHAR", "acceptance_ref": "JSON", "verification": "VARCHAR",
    "verification_ref": "JSON", "installation": "VARCHAR",
    "lifecycle_basis": "VARCHAR", "installation_ref": "JSON", "parse_status": "VARCHAR",
    "text_chars": "BIGINT", "source": "VARCHAR",
}
ITEM_COLUMNS = {
    "item_id": "VARCHAR", "summary_id": "VARCHAR", "ordinal": "BIGINT",
    "section": "VARCHAR", "subfield": "VARCHAR", "scope": "VARCHAR", "kind": "VARCHAR",
    "text": "VARCHAR", "status": "VARCHAR", "attribution": "VARCHAR", "pointer": "VARCHAR",
    "parse_status": "VARCHAR",
}
REF_COLUMNS = {
    "ref_id": "VARCHAR", "item_id": "VARCHAR", "summary_id": "VARCHAR", "label": "VARCHAR",
    "snapshot_id": "VARCHAR", "resolved_id": "VARCHAR", "resolved_ref": "JSON",
    "status": "VARCHAR", "basis": "VARCHAR",
}
STRUCTURED_SECTIONS = (
    ("objective", "objective"), ("constraints", "constraint"), ("completedWork", "completed"),
    ("currentState", "current_state"), ("decisions", "decision"), ("openIssues", "open_issue"),
    ("nextActions", "next_action"), ("criticalContext", "context"),
    ("modifiedFiles", "modified_path"), ("referencedFiles", "referenced_path"),
    ("verificationState", "verification"),
)
CONTINUATION_FIELDS = {
    "currentGoal": ("objective", "objective"), "currentApproach": ("approach", "approach"),
    "immediateNextStep": ("next_action", "next_action"),
}
SHADOW_TITLES = {
    "objective": ("objective", "objective"), "constraints": ("constraints", "constraint"),
    "completed work": ("completedWork", "completed"), "current state": ("currentState", "current_state"),
    "decisions": ("decisions", "decision"), "open issues": ("openIssues", "open_issue"),
    "next actions": ("nextActions", "next_action"), "critical context": ("criticalContext", "context"),
    "modified files": ("modifiedFiles", "modified_path"),
    "referenced files": ("referencedFiles", "referenced_path"),
    "verification state": ("verificationState", "verification"), "continuation": ("continuation", "unknown"),
}
PI_TITLES = {
    "goal": ("Goal", "objective"), "constraints & preferences": ("Constraints & Preferences", "constraint"),
    "progress": ("Progress", "progress"), "key decisions": ("Key Decisions", "decision"),
    "next steps": ("Next Steps", "next_action"), "critical context": ("Critical Context", "context"),
    "turn context (split turn)": ("Turn Context", "split"),
    "original request": ("Original Request", "objective"),
    "progress so far": ("Progress So Far", "completed"),
    "context needed to continue": ("Context Needed to Continue", "context"),
    "verification state": ("verificationState", "verification"),
    "modified files": ("modifiedFiles", "modified_path"),
    "referenced files": ("referencedFiles", "referenced_path"),
}
CLAUDE_TITLES = {
    "what this work is for": ("what this work is for", "context"),
    "primary request and intent": ("Primary Request and Intent", "objective"),
    "key technical concepts": ("Key Technical Concepts", "context"),
    "files and code sections": ("Files and Code Sections", "context"),
    "errors and fixes": ("Errors and fixes", "pitfall"),
    "problem solving": ("Problem Solving", "decision"),
    "all user messages": ("All user messages", "reported_user_message"),
    "pending tasks": ("Pending Tasks", "open_issue"),
    "current work": ("Current Work", "current_state"),
    "optional next step": ("Optional Next Step", "next_action"),
}
REF_LABEL = re.compile(r"^E(\d{4,})$")
PI_TITLE = re.compile(r"^##\s+(.+?)\s*#*\s*$")
PI_SUBTITLE = re.compile(r"^###\s+(.+?)\s*#*\s*$")
CLAUDE_TITLE = re.compile(r"^(\d+)[.)]\s*(.+?):?\s*$")
CLAUDE_HEADING = re.compile(r"^##\s+(.+?)\s*#*\s*$")
LIST_ITEM = re.compile(r"^(\s*)[-*+]\s+(.*)$")
NUMBERED_ITEM = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
CHECKBOX = re.compile(r"^\[[ xX]\]\s*(.*)$")
VERIFY_TAG = re.compile(r"^\[(verified|unverified|blocked|done|in_progress|unknown)\]\s*(.*)$", re.IGNORECASE)
FENCE = re.compile(r"^\s*(```|~~~)")
SPLIT_MARK = re.compile(r"^\*\*Turn Context \(split turn\):\*\*\s*$")
CLAUDE_FOOTER = re.compile(r"^If you need specific details from before compaction", re.IGNORECASE)
SENTINEL_EMPTY = re.compile(r"^\(none\)$", re.IGNORECASE)
STATUSES = {"verified", "unverified", "blocked", "done", "in_progress", "unknown"}


def _payload(event, cache):
    """The original decoded record for one event; text fallback keeps partial data visible."""
    try:
        return pointer(cache.get(event["ref"]), event.get("payload_pointer", ""))
    except (KeyError, IndexError, TypeError, ValueError, OSError):
        text = event.get("text")
        if isinstance(text, str):
            try:
                return json.loads(text)
            except ValueError:
                return text
        return None


def _json_text(value):
    return json.dumps(value, ensure_ascii=True) if isinstance(value, (dict, list)) else str(value)


def _normalize(text):
    return " ".join(str(text or "").split())


def _label_list(value):
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def structured_items(structured, base="#/structured"):
    """Original section items and continuation subfields from one structured checkpoint.

    Each recognized item keeps its exact recorded text. A nested ``{text,
    evidenceRefs, status}`` object is the documented v3 shape; older flat strings
    stay readable and are marked ``legacy``. Unknown keys are preserved as
    unknown-kind items rather than silently dropped.
    """
    if not isinstance(structured, dict):
        return []
    items = []

    def item(section, subfield, kind, text, status=None, labels=(), pointer_path="", shape="documented",
             scope="history"):
        text = text if isinstance(text, str) else _json_text(text)
        items.append({"section": section, "subfield": subfield, "kind": kind, "text": text, "scope": scope,
                      "status": status if status in STATUSES else "unknown",
                      "labels": list(dict.fromkeys(labels)), "pointer": pointer_path,
                      "parse_status": "complete" if shape == "documented" else "partial"})

    for key, kind in STRUCTURED_SECTIONS:
        value = structured.get(key)
        if value in (None, "", [], {}):
            continue
        values = value if isinstance(value, list) else [value]
        for index, entry in enumerate(values):
            location = f"{base}/{key}/{index}"
            if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                status = entry.get("status") if isinstance(entry.get("status"), str) else None
                item(key, None, kind, entry["text"], status, _label_list(entry.get("evidenceRefs")), location)
            elif isinstance(entry, str):
                item(key, None, kind, entry, None, (), location, "legacy")
            else:
                item(key, None, kind, entry, None, (), location, "malformed")
    continuation = structured.get("continuationState")
    if isinstance(continuation, dict):
        for key, value in continuation.items():
            kind = CONTINUATION_FIELDS.get(key, ("unknown", "unknown"))[1]
            if isinstance(value, dict) and isinstance(value.get("text"), str):
                status = value.get("status") if isinstance(value.get("status"), str) else None
                item("continuationState", key, kind, value["text"], status,
                     _label_list(value.get("evidenceRefs")), f"{base}/continuationState/{key}",
                     scope="continuation")
            elif isinstance(value, str):
                if value.strip():
                    item("continuationState", key, kind, value, None, (),
                         f"{base}/continuationState/{key}", scope="continuation")
            elif value not in (None, "", [], {}):
                item("continuationState", key, kind, value, None, (),
                     f"{base}/continuationState/{key}", "malformed", "continuation")
    known = {key for key, _ in STRUCTURED_SECTIONS} | {"continuationState", "version"}
    for key, value in structured.items():
        if key in known or value in (None, "", [], {}):
            continue
        values = value if isinstance(value, list) else [value]
        for index, entry in enumerate(values):
            location = f"{base}/{key}/{index}" if isinstance(value, list) else f"{base}/{key}"
            if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                status = entry.get("status") if isinstance(entry.get("status"), str) else None
                item(key, None, "unknown", entry["text"], status,
                     _label_list(entry.get("evidenceRefs")), location)
            else:
                item(key, None, "unknown", entry, None, (), location,
                     "documented" if isinstance(entry, str) else "malformed")
    return items


class _Markdown:
    """Block-level Markdown state shared by the recognized summary layouts.

    Headings and list markers inside fenced code stay text. Blocks keep their
    original lines; a nested list entry is its own item, not a rewrite of its
    parent. Unknown headings remain visible with ``kind=unknown``.
    """

    def __init__(self, text, first_line):
        self.text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        self.first_line = first_line or 0
        self.items = []
        self.unknown = False
        self.fence = False
        self.block = None
        self.scope = "wrapper"
        self.section = "preamble"
        self.subfield = None
        self.kind = "context"
        self.status = "unknown"
        self.section_status = "unknown"
        self.attribution = "rendered"
        self.file_tag = False

    def line(self, index):
        """Absolute recorded line for a zero-based text line."""
        return (self.first_line or 0) + index

    def flush(self):
        if not self.block:
            return
        text = "\n".join(self.block["lines"]).strip()
        start, end = self.block["start"], self.block["end"]
        if text and not SENTINEL_EMPTY.match(text):
            self.items.append({"section": self.section, "subfield": self.subfield,
                               "scope": self.block.get("scope", self.scope), "kind": self.block.get("kind", self.kind),
                               "text": text, "status": self.block.get("status", self.status),
                               "pointer": f"line:{start}-{end}" if end > start else f"line:{start}",
                               "parse_status": self.block.get("parse_status", "complete"),
                               "attribution": self.attribution})
        self.block = None

    def start(self, text, line, kind=None, status=None, scope=None):
        self.flush()
        self.block = {"lines": [text], "start": line, "end": line, "kind": kind or self.kind,
                      "status": status or self.status, "scope": scope or self.scope,
                      "parse_status": "complete"}

    def append(self, text, line):
        if self.block is None:
            self.start(text, line)
        else:
            self.block["lines"].append(text)
            self.block["end"] = line

    def finish(self):
        self.flush()
        return self.items


def _strip_checkbox(text):
    found = CHECKBOX.match(text.strip())
    return found.group(1) if found else text


def _shadow_items(text, first_line):
    md = _Markdown(text, first_line)
    md.attribution = "rendered"
    for index, raw in enumerate(md.text.split("\n")):
        line = md.line(index)
        if FENCE.match(raw):
            md.fence = not md.fence
            md.append(raw, line)
            continue
        if md.fence:
            md.append(raw, line)
            continue
        heading = re.match(r"^##\s+(.+?)\s*#*\s*$", raw)
        if heading:
            md.flush()
            md.scope = "history"
            title = _normalize(heading.group(1)).casefold()
            found = SHADOW_TITLES.get(title)
            if found is None:
                md.unknown = True
                md.section, md.kind = "unknown", "unknown"
            else:
                md.section, md.kind = found
            md.subfield = None
            md.status = md.section_status = "unknown"
            continue
        if raw.strip() == "# Continuation Checkpoint":
            md.flush()
            continue
        listed = LIST_ITEM.match(raw)
        if listed:
            content = listed.group(2).strip()
            status = "unknown"
            if md.section == "verificationState":
                found = VERIFY_TAG.match(content)
                if found:
                    status = found.group(1).casefold()
                    content = found.group(2)
            if md.section == "continuation":
                for prefix, subfield, kind in (("Goal:", "currentGoal", "objective"),
                                               ("Approach:", "currentApproach", "approach"),
                                               ("Next:", "immediateNextStep", "next_action")):
                    if content.startswith(prefix):
                        md.flush()
                        md.subfield, md.kind = subfield, kind
                        md.start(content[len(prefix):].strip(), line, kind, status, "continuation")
                        break
                else:
                    md.start(content, line, "unknown", status, "continuation")
                continue
            kind = "verification" if md.section == "verificationState" else md.kind
            md.start(content, line, kind, status)
            continue
        if not raw.strip():
            md.flush()
            continue
        md.append(raw, line)
    return md.finish(), md.unknown


def _pi_items(text, first_line):
    md = _Markdown(text, first_line)
    md.attribution = "native"
    for index, raw in enumerate(md.text.split("\n")):
        line = md.line(index)
        if FENCE.match(raw):
            md.fence = not md.fence
            md.append(raw, line)
            continue
        if md.fence:
            md.append(raw, line)
            continue
        if SPLIT_MARK.match(raw):
            md.flush()
            md.scope, md.section, md.kind, md.subfield = "turn_prefix", "turn_prefix", "context", None
            continue
        if re.match(r"^-{3,}\s*$", raw):
            md.flush()
            continue
        opened = re.match(r"^<(read|modified)-files>\s*$", raw)
        if opened:
            md.flush()
            md.section = "readFiles" if opened.group(1) == "read" else "modifiedFiles"
            md.kind = "referenced_path" if opened.group(1) == "read" else "modified_path"
            md.scope, md.subfield, md.file_tag = "history", None, True
            continue
        if re.match(r"^</(read|modified)-files>\s*$", raw):
            md.flush()
            md.file_tag = False
            continue
        if md.file_tag:
            if raw.strip():
                md.start(raw.strip(), line, md.kind, "unknown")
            continue
        heading = PI_TITLE.match(raw)
        if heading:
            md.flush()
            md.scope = "history"
            title = _normalize(heading.group(1)).casefold()
            found = PI_TITLES.get(title)
            if found is None:
                md.unknown = True
                md.section, md.kind = "unknown", "unknown"
            elif found[1] == "split":
                md.scope, md.section, md.kind = "turn_prefix", "turn_prefix", "context"
            else:
                md.section, md.kind = found
                if md.section in ("Original Request", "Progress So Far", "Context Needed to Continue"):
                    md.scope = "turn_prefix"
            md.subfield, md.status, md.section_status = None, "unknown", "unknown"
            continue
        subtitle = PI_SUBTITLE.match(raw)
        if subtitle:
            md.flush()
            title = _normalize(subtitle.group(1)).casefold()
            if title in ("done", "in progress", "blocked"):
                md.subfield = title
                md.section_status = {"done": "done", "in progress": "in_progress", "blocked": "blocked"}[title]
                md.kind = {"done": "completed", "in progress": "in_progress", "blocked": "blocked"}[title]
            else:
                md.unknown = True
                md.subfield = title
                md.kind = "unknown"
            md.status = md.section_status
            continue
        listed = LIST_ITEM.match(raw)
        if listed:
            content = _strip_checkbox(listed.group(2).strip())
            status = md.section_status
            kind = md.kind
            if md.section == "verificationState":
                found = VERIFY_TAG.match(content)
                if found:
                    status = found.group(1).casefold()
                    content = found.group(2)
                kind = "verification"
            md.start(content, line, kind, status)
            continue
        numbered = NUMBERED_ITEM.match(raw)
        if numbered and not numbered.group(1):
            md.start(numbered.group(2).strip(), line, md.kind, md.section_status)
            continue
        if not raw.strip():
            md.flush()
            continue
        md.append(raw, line)
    return md.finish(), md.unknown


def _claude_section(raw):
    """Known Claude section title from a heading or top-level numbered label."""
    match = CLAUDE_HEADING.match(raw)
    body = match.group(1) if match else None
    if body is None:
        match = CLAUDE_TITLE.match(raw)
        body = match.group(2) if match else None
    if body is None:
        return None
    body = re.sub(r"^\d+[.)]\s+", "", body.strip())
    body = body.strip().rstrip(":").strip().strip("*").strip()
    return CLAUDE_TITLES.get(_normalize(body).casefold())


def _claude_items(text, first_line):
    md = _Markdown(text, first_line)
    md.attribution = "native"
    in_preamble = True
    for index, raw in enumerate(md.text.split("\n")):
        line = md.line(index)
        if FENCE.match(raw):
            md.fence = not md.fence
            md.append(raw, line)
            continue
        if md.fence:
            md.append(raw, line)
            continue
        if CLAUDE_FOOTER.match(raw.strip()):
            md.flush()
            md.section, md.kind, md.subfield = "footer", "context", None
            md.scope = "wrapper"
            md.start(raw.strip(), line, "context", "unknown", "wrapper")
            continue
        found = _claude_section(raw)
        if found is not None:
            md.flush()
            in_preamble = False
            md.section, md.kind = found
            md.scope, md.subfield, md.status, md.section_status = "history", None, "unknown", "unknown"
            continue
        if re.match(r"^\d+[.)]\s+\*\*.+\*\*\s*:?\s*$", raw):
            md.flush()
            in_preamble = False
            md.unknown = True
            md.section, md.kind = "unknown", "unknown"
            md.scope, md.subfield, md.status, md.section_status = "history", None, "unknown", "unknown"
            continue
        if in_preamble:
            md.section, md.kind = "preamble", "context"
            md.scope = "wrapper"
        listed = LIST_ITEM.match(raw)
        if listed:
            md.start(_strip_checkbox(listed.group(2).strip()), line, md.kind, md.section_status)
            continue
        numbered_item = NUMBERED_ITEM.match(raw)
        if numbered_item and not numbered_item.group(1):
            md.start(numbered_item.group(2).strip(), line, md.kind, md.section_status)
            continue
        if not raw.strip():
            md.flush()
            continue
        md.append(raw, line)
    return md.finish(), md.unknown


def _ancestry(recording):
    """Message id -> parent over every readable record, plus event rows and record refs.

    The reader keeps a record-level index because a profile may emit no event for a
    record (an empty assistant error, for example); a branch position must still walk it.
    """
    by_id, record_refs = {}, {}
    for event in recording["events"]:
        identity = event.get("external_message_id")
        if identity:
            by_id.setdefault(identity, []).append(event)
    indexed = recording.get("record_ancestry") or {}
    if indexed:
        parent = {identity: item.get("parent") for identity, item in indexed.items()}
        record_refs = {identity: item.get("ref") for identity, item in indexed.items() if item.get("ref")}
        return parent, by_id, set(recording.get("record_conflicts") or ()), record_refs
    parent, conflicts = {}, set()
    for identity, events in by_id.items():
        recorded = {event.get("external_parent_id") for event in events}
        if len(recorded) > 1:
            conflicts.add(identity)
        else:
            parent[identity] = next(iter(recorded))
    return parent, by_id, conflicts, record_refs


def _branch(leaf, parent, conflicts):
    """Recorded ancestry from root through leaf, or (None, reason) when it cannot be proven."""
    if not leaf:
        return None, "no_leaf"
    seen, order, current = set(), [], leaf
    while current is not None:
        if current in seen:
            return None, "cycle"
        if current in conflicts:
            return None, "ambiguous"
        if current not in parent:
            return None, "missing_ancestry"
        seen.add(current)
        order.append(current)
        current = parent[current]
    order.reverse()
    return order, "complete"


def _coverage_status(end, leaf, parent, conflicts):
    if not end:
        return "unknown", "no_recorded_coverage_end"
    branch, reason = _branch(leaf, parent, conflicts)
    if branch is None:
        return "unknown", reason
    if end in branch:
        return "resolved", f"on_branch:{leaf}"
    return "off_branch", f"not_on_branch:{leaf}"


def _occurrence(event):
    """(physical_kind, purpose) for one recognized summary-ish event."""
    native = event.get("native_type")
    purpose = event.get("purpose")
    if event.get("kind") == "checkpoint":
        return "checkpoint", purpose or "checkpoint"
    if event.get("harness") == "claude-code" or native in ("system", "summary"):
        if native == "system" and event.get("native_subtype") == "compact_boundary":
            return "compact_boundary", "compaction"
        if native == "summary":
            return "summary", purpose or "summary"
        return "compact_summary", purpose or "compaction"
    if native == "branch_summary":
        return "branch_summary", "branch"
    return "compaction", purpose or "compaction"


def _representation(event, text, physical):
    """Recognized representation and producer. A heading alone never authenticates Shadow."""
    structured = event.get("kind") == "checkpoint"
    if structured:
        return "structured_json", "shadow", "shadow_structured_envelope"
    if physical == "compact_boundary":
        return "boundary_metadata", "harness_native", "recorded_compact_boundary"
    if event.get("checkpoint_id"):
        return "rendered_markdown", "shadow", "recorded_shadow_compaction_metadata"
    body = str(text or "")
    if "# Continuation Checkpoint" in body and re.search(r"^##\s+Objective\s*$", body, re.MULTILINE):
        return "rendered_markdown", "unknown", "recognized_shadow_layout_only"
    if re.search(r"^##\s+(?:Goal|Constraints & Preferences|Progress|Key Decisions|Next Steps|Critical Context)\s*$",
                 body, re.MULTILINE):
        return "native_markdown", "harness_native", "recognized_pi_layout"
    if event.get("harness") == "claude-code" and any(_claude_section(line) for line in body.splitlines()):
        return "native_markdown", "harness_native", "recognized_claude_layout"
    return "opaque", "unknown", "unrecognized_layout"


def _parse(event, text, representation):
    """(items, unknown_section) for a recognized Markdown representation."""
    first_line = (event.get("ref") or {}).get("line") or 0
    if representation == "rendered_markdown":
        return _shadow_items(text, first_line)
    if event.get("harness") == "claude-code":
        return _claude_items(text, first_line)
    return _pi_items(text, first_line)


def _prior_checkpoint(rows, recording_id, order, lane):
    """Nearest earlier checkpoint on the same recorded lane; another lane is not the served one."""
    candidates = [row for row in rows
                  if row.get("physical_kind") == "checkpoint" and row.get("recording_id") == recording_id
                  and row.get("order") is not None and order is not None and row["order"] < order]
    if lane:
        candidates = [row for row in candidates if row.get("lane") == lane]
    elif len(candidates) > 1:
        return None
    return max(candidates, key=lambda row: row["order"]) if candidates else None


def _resolve_refs(rows, items_by_summary, refs, parent, by_id, conflicts, record_refs):
    """Resolve Pi positional labels inside the exact prepared snapshot; never guess a missing map."""
    checkpoints = [row for row in rows if row.get("physical_kind") == "checkpoint"]
    branches = {}
    for row in rows:
        summary_id = row["summary_id"]
        for item in items_by_summary.get(summary_id, []):
            for index, label in enumerate(item.pop("labels", [])):
                ref_id = f"{item['item_id']}#{index}"
                base = {"ref_id": ref_id, "item_id": item["item_id"], "summary_id": summary_id,
                        "label": label, "snapshot_id": row.get("snapshot_id"),
                        "resolved_id": None, "resolved_ref": None}
                if row.get("harness") != "pi":
                    refs.append({**base, "status": "unsupported", "basis": "no_recorded_snapshot_map"})
                    continue
                leaf = row.get("prepared_leaf_id") or row.get("coverage_end_id")
                basis = "prepared_leaf" if row.get("prepared_leaf_id") else "coverage_end_fallback"
                if not leaf:
                    refs.append({**base, "status": "missing_snapshot", "basis": "no_prepared_leaf"})
                    continue
                key = leaf
                if key not in branches:
                    branches[key] = _branch(key, parent, conflicts)
                found, reason = branches[key]
                if found is None:
                    refs.append({**base, "status": "missing_snapshot", "basis": reason})
                    continue
                if label == "E0000":
                    previous = _prior_checkpoint(checkpoints, row.get("recording_id"), row.get("order"),
                                                 row.get("lane"))
                    if previous is None:
                        refs.append({**base, "status": "prior_summary",
                                     "basis": "no_earlier_checkpoint_on_lane"})
                    else:
                        refs.append({**base, "status": "prior_summary", "resolved_id": previous.get("producer_id"),
                                     "resolved_ref": previous.get("installation_ref") or previous.get("ref"),
                                     "basis": basis})
                    continue
                match = REF_LABEL.match(label)
                position = int(match.group(1)) - 1 if match else None
                if position is None:
                    refs.append({**base, "status": "unsupported", "basis": "unrecognized_label"})
                elif position < 0 or position >= len(found):
                    refs.append({**base, "status": "missing_entry", "basis": f"branch_positions:{len(found)}"})
                else:
                    identity = found[position]
                    resolved = by_id.get(identity) or []
                    ref = resolved[0].get("ref") if resolved else record_refs.get(identity)
                    refs.append({**base, "status": "resolved", "resolved_id": identity,
                                 "resolved_ref": ref, "basis": f"{basis}:{leaf}"})


def _twin_status(rows, items_by_summary):
    """Group proven same-session/producer twins; disagreement stays visible, never merged."""
    groups = {}
    for row in rows:
        if row.get("twin_key"):
            groups.setdefault(row["twin_key"], []).append(row)
    for key, members in groups.items():
        signatures = {}
        for row in members:
            if row.get("parse_status") == "unsupported":
                continue
            signature = tuple(sorted((_normalize(item.get("text")), item.get("status"), item.get("scope"))
                                     for item in items_by_summary.get(row["summary_id"], [])
                                     if item.get("scope") != "wrapper"))
            signatures.setdefault(signature, []).append(row)
        supported = [row for row in members if row.get("parse_status") != "unsupported"]
        for row in members:
            if row.get("parse_status") == "unsupported":
                row["twin_status"] = "unsupported"
            elif len(supported) == 1:
                row["twin_status"] = "only"
            elif len(signatures) == 1:
                row["twin_status"] = "paired_consistent"
            else:
                row["twin_status"] = "paired_mismatch"


def build(recordings, cache, checkpoints):
    """(summaries, summary_items, summary_refs, coverage) across scanned recordings."""
    rows, items_by_summary, refs = [], {}, []
    coverage = {"complete": True, "occurrences": 0, "structured": 0, "rendered": 0, "native": 0,
                "boundary": 0, "opaque": 0, "items": 0, "refs": 0, "refs_resolved": 0,
                "refs_missing_snapshot": 0, "refs_missing_entry": 0, "twins_paired": 0,
                "twins_mismatch": 0, "unknown_sections": 0, "partial": 0, "unsupported": 0}
    checkpoint_by_event = {(row.get("recording_id"), row.get("publication_event_id")): row for row in checkpoints}
    for recording in recordings:
        events = recording["events"]
        parent, by_id, conflicts, record_refs = _ancestry(recording)
        recording_rows = []
        boundaries = {}
        for event in events:
            if event.get("kind") == "summary" and event.get("native_type") == "system" \
                    and event.get("native_subtype") == "compact_boundary":
                boundaries.setdefault(event.get("anchor_id"), []).append(event)
        for event in events:
            if event.get("kind") not in ("checkpoint", "summary"):
                continue
            if event.get("harness") not in ("pi", "claude-code"):
                continue  # Only the two recorded summary formats this decoder owns.
            if event.get("kind") == "summary" and event.get("purpose") not in ("compaction", "branch", "summary"):
                continue
            physical, purpose = _occurrence(event)
            text = event.get("text") or ""
            representation, producer, producer_basis = _representation(event, text, physical)
            decoded = _payload(event, cache) if event.get("kind") == "checkpoint" else None
            schema_version = event.get("schema_version")
            if schema_version is None and isinstance(decoded, dict):
                schema_version = decoded.get("version")
            row = {
                "summary_id": f"{event.get('recording_id')}#{event.get('event_id')}",
                "recording_id": event.get("recording_id"), "session": event.get("session"),
                "independent_session": event.get("independent_session"), "harness": event.get("harness"),
                "lane": event.get("lane"), "branch": event.get("branch"), "order": event.get("order"),
                "timestamp": event.get("timestamp"), "event_id": event.get("event_id"),
                "ref": event.get("ref"), "physical_kind": physical, "purpose": purpose,
                "representation": representation, "producer": producer, "producer_basis": producer_basis,
                "producer_id": event.get("checkpoint_id") or event.get("anchor_id"),
                "schema_version": str(schema_version) if schema_version is not None else None,
                "parser_version": PARSER_VERSION,
                "coverage_start_id": event.get("coverage_start_id"),
                "coverage_end_id": event.get("coverage_end_id"),
                "prepared_leaf_id": event.get("prepared_at_leaf"),
                "published_leaf_id": event.get("published_at_leaf"),
                "first_kept_id": event.get("first_kept_id"),
                "retained_anchor": event.get("retained_anchor"),
                "anchor_id": event.get("anchor_id"),
                "logical_parent_id": event.get("logical_parent_id"),
                "retained_ids": event.get("retained_ids"),
                "compact_trigger": event.get("compact_trigger"), "pre_tokens": event.get("pre_tokens"),
                "duration_ms": event.get("duration_ms"),
                "preserved_head_id": event.get("preserved_head_id"),
                "preserved_tail_id": event.get("preserved_tail_id"),
                "parse_status": "complete", "text_chars": event.get("text_chars"),
                "source": "recorded_summary",
            }
            if event.get("kind") == "checkpoint":
                checkpoint = checkpoint_by_event.get((event.get("recording_id"), event.get("event_id")))
                if checkpoint:
                    row["coverage_end_id"] = checkpoint.get("coverage_end_id")
                    row["coverage_start_id"] = checkpoint.get("coverage_start_id") or row["coverage_start_id"]
                    # Branch positions are proven over every recorded parent link, including
                    # records with no projected event. The operation-fact boundary remains its own view.
                    row["coverage_status"], row["coverage_basis"] = _coverage_status(
                        row["coverage_end_id"], row.get("prepared_leaf_id") or row["coverage_end_id"],
                        parent, conflicts)
                    leaf = row.get("prepared_leaf_id") or row.get("coverage_end_id")
                    row["snapshot_id"] = f"{event.get('recording_id')}#branch:{leaf}" if leaf else None
                    row["acceptance"] = "unknown"
                    row["verification"] = "unknown"
                    row["installation"] = "custom_only"
                    row["lifecycle_basis"] = "custom_entry_without_authoritative_install_confirmation"
                    row["installation_ref"] = event.get("ref")
            elif physical == "compact_boundary":
                row["coverage_status"], row["coverage_basis"] = "metadata_only", "compact_boundary_metadata"
                row["acceptance"] = "unknown"
                row["verification"] = "unknown"
                row["installation"] = "canonical_compaction"
                row["lifecycle_basis"] = "recorded_compact_boundary"
                row["installation_ref"] = event.get("ref")
            elif event.get("harness") == "claude-code":
                paired = max((item for item in boundaries.get(event.get("anchor_id"), [])
                              if (item.get("order") or 0) < (event.get("order") or 0)),
                             key=lambda item: item.get("order") or 0, default=None)
                if paired is not None:
                    row["first_kept_id"] = row["first_kept_id"] or paired.get("preserved_head_id")
                    row["retained_ids"] = row["retained_ids"] or paired.get("retained_ids")
                    row["coverage_status"] = "metadata_only"
                    row["coverage_basis"] = "paired_compact_boundary"
                    row["acceptance"] = "unknown"
                    row["installation"] = "canonical_compaction"
                    row["installation_ref"] = paired.get("ref")
                    row["lifecycle_basis"] = "body_matched_to_recorded_boundary_anchor"
                else:
                    row["coverage_status"] = "unknown"
                    row["acceptance"] = "unknown"
                    row["installation"] = "unconfirmed"
                    row["installation_ref"] = event.get("ref")
                    row["lifecycle_basis"] = "summary_body_without_matching_boundary"
                row["verification"] = "unknown"
            else:
                shadow_end = event.get("shadow_coverage_end")
                leaf = event.get("prepared_at_leaf") or shadow_end
                row["coverage_end_id"] = shadow_end
                row["coverage_status"], row["coverage_basis"] = _coverage_status(shadow_end, leaf, parent, conflicts)
                row["snapshot_id"] = f"{event.get('recording_id')}#branch:{leaf}" if leaf else None
                if event.get("producer_id"):
                    row["acceptance"] = "unknown"
                    row["installation"] = "canonical_compaction"
                    row["installation_ref"] = event.get("ref")
                    row["lifecycle_basis"] = "recorded_compaction_with_shadow_metadata"
                else:
                    row["acceptance"] = "unknown"
                    row["installation"] = "canonical_compaction"
                    row["installation_ref"] = event.get("ref")
                    row["lifecycle_basis"] = "recorded_compaction_occurrence"
                row["verification"] = "unknown"
            if physical in ("compaction", "branch_summary", "checkpoint") and row.get("producer_id"):
                row["twin_key"] = f"{row.get('session')}|{row['producer_id']}"
            if physical == "checkpoint" or representation == "structured_json":
                structured = decoded
                base = "#" + (event.get("payload_pointer") or "/structured")
                items = structured_items(structured, base)
                if not isinstance(structured, dict):
                    row["parse_status"], items = "unsupported", []
                elif not items:
                    row["parse_status"] = "partial"
                elif row["schema_version"] not in (None, "1", "2", "3"):
                    # A newer structured version is still preserved, but not claimed as fully decoded.
                    row["parse_status"] = "partial"
            elif representation in ("rendered_markdown", "native_markdown"):
                items, unknown = _parse(event, text, representation)
                if unknown:
                    row["parse_status"], coverage["unknown_sections"] = "partial", coverage["unknown_sections"] + 1
                if not items:
                    row["parse_status"] = "unsupported"
            elif representation == "boundary_metadata":
                items = []
            else:
                items, row["parse_status"] = [], "unsupported"
            if not items and representation in ("rendered_markdown", "native_markdown"):
                row["parse_status"] = "unsupported"
            if row.get("coverage_status") is None:
                row["coverage_status"] = "unknown"
            if row.get("coverage_basis") is None:
                row["coverage_basis"] = "no_recorded_coverage"
            row["citation_availability"] = ("item" if any(item.get("labels") for item in items)
                                            else "none" if items else "unknown")
            coverage["occurrences"] += 1
            coverage[{"structured_json": "structured", "rendered_markdown": "rendered",
                      "native_markdown": "native", "boundary_metadata": "boundary",
                      "opaque": "opaque"}.get(representation, "opaque")] += 1
            rows.append(row)
            recording_rows.append(row)
            summary_items = []
            for ordinal, item in enumerate(items):
                item_id = f"{row['summary_id']}#item:{ordinal}"
                entry = {"item_id": item_id, "summary_id": row["summary_id"], "ordinal": ordinal,
                         "section": item["section"], "subfield": item.get("subfield"),
                         "scope": item.get("scope", "history"), "kind": item["kind"],
                         "text": item["text"], "status": item.get("status") or "unknown",
                         "attribution": item.get("attribution") or ("structured"
                                                                    if representation == "structured_json"
                                                                    else "native"),
                         "pointer": item.get("pointer"), "parse_status": item.get("parse_status", "complete"),
                         "labels": item.get("labels", [])}
                summary_items.append(entry)
            items_by_summary[row["summary_id"]] = summary_items
            coverage["items"] += len(summary_items)
        _resolve_refs(recording_rows, items_by_summary, refs, parent, by_id, conflicts, record_refs)
    for row in rows:
        row.setdefault("twin_key", None)
        row.setdefault("twin_status", "only")
    _twin_status(rows, items_by_summary)
    # A recorded compaction twin is authoritative installation evidence for the
    # custom checkpoint it names; the custom entry alone is not.
    groups = {}
    for row in rows:
        if row.get("twin_key"):
            groups.setdefault(row["twin_key"], []).append(row)
    for members in groups.values():
        installed = next((row for row in members if row.get("physical_kind") != "checkpoint"
                          and row.get("installation") in ("canonical_compaction", "canonical_summary")), None)
        if installed is None:
            continue
        for row in members:
            if row.get("physical_kind") == "checkpoint" and row.get("installation") == "custom_only":
                row["installation"] = installed["installation"]
                row["installation_ref"] = installed.get("installation_ref")
                row["lifecycle_basis"] = "matching_recorded_compaction_twin"
    for row in rows:
        if row.get("twin_status") == "paired_consistent":
            coverage["twins_paired"] += 1
        elif row.get("twin_status") == "paired_mismatch":
            coverage["twins_mismatch"] += 1
    for ref in refs:
        coverage["refs"] += 1
        if ref["status"] == "resolved":
            coverage["refs_resolved"] += 1
        elif ref["status"] == "missing_snapshot":
            coverage["refs_missing_snapshot"] += 1
        elif ref["status"] == "missing_entry":
            coverage["refs_missing_entry"] += 1
    for row in rows:
        items = items_by_summary.get(row["summary_id"], [])
        for item in items:
            if item["parse_status"] != "complete":
                row["parse_status"] = "partial" if row["parse_status"] != "unsupported" else "unsupported"
        if row["parse_status"] == "partial":
            coverage["partial"] += 1
        elif row["parse_status"] == "unsupported":
            coverage["unsupported"] += 1
    coverage["complete"] = not (coverage["opaque"] or coverage["twins_mismatch"]
                                or coverage["refs_missing_snapshot"] or coverage["refs_missing_entry"]
                                or coverage["unsupported"])
    coverage["source"] = "recorded summary occurrences; claims stay attributed"
    return rows, [item for items in items_by_summary.values() for item in items], refs, coverage
