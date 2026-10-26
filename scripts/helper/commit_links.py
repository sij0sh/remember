"""Commit witnesses derived from already-scanned session events; nothing is rescanned.

A commit-producing command is paired with its recorded result: the new SHA comes
from ``[branch sha]``, ``commit <sha>``, a one-line ``git log`` entry, or ``HEAD
is now at``. Quiet commits with no recorded SHA may be resolved by subject against
the command's local history; those witnesses are marked ``inferred``, and a subject
shared by several nearby commits stays ``ambiguous_subject``. Commands whose SHA
cannot be recovered stay as ``unresolved`` witnesses with a null SHA.
"""
import datetime as dt

from .git_commands import (assign_shas, command_cwd, commit_contexts, extract_message,
                           normalize_subject, output_shas)

GRAIN = "one event-to-commit witness"
# Subject matches farther than this from the recorded command are not candidates.
WINDOW_SECONDS = 24 * 3600
COLUMNS = {"recording_id": "VARCHAR", "event_id": "VARCHAR", "session": "VARCHAR", "harness": "VARCHAR",
           "timestamp": "VARCHAR", "repo_id": "VARCHAR", "repo": "VARCHAR", "command_cwd": "VARCHAR",
           "sha": "VARCHAR", "recorded_sha": "VARCHAR", "subject": "VARCHAR", "command_subject": "VARCHAR",
           "command_offset": "BIGINT", "basis": "VARCHAR", "confidence": "VARCHAR", "failed": "BOOLEAN",
           "command": "VARCHAR", "output_excerpt": "VARCHAR", "ref": "JSON", "result_ref": "JSON"}


def _subject_match(resolver, repo, subject, when):
    """(sha, subject, basis) for one unambiguous subject match near the event, else basis only."""
    rows = resolver.subject(repo, subject)
    stamp = None
    if when:
        try:
            stamp = dt.datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    if stamp is not None:
        rows = [row for row in rows if abs(row[2] - stamp) <= WINDOW_SECONDS]
    if len(rows) == 1:
        return rows[0][0], rows[0][1], "repo_subject"
    return None, None, "ambiguous_subject" if rows else None


def _event_witnesses(event, recording, by_ref, cache, resolver, fallback):
    command = event.get("command") or cache.payload(event)
    contexts = commit_contexts(command)
    if not contexts:
        return []
    result_ref = event.get("result_ref") or {}
    result_event = by_ref.get((result_ref.get("offset"), result_ref.get("item", 0)))
    output = cache.payload(result_event) if result_event else cache.payload(event)
    direct = any(item[1] for item in contexts)
    assignments = assign_shas(contexts, output_shas(output, strong_only=not direct))
    failed = event.get("outcome") == "failed" or event.get("exit_code") not in (None, 0)
    if failed:
        # A failed command may still print a commit header; nothing weaker counts.
        assignments = [c if c and c[1] in ("commit_output", "head") else None for c in assignments]
    direct_offsets = [offset for offset, is_direct, text in contexts if is_direct and text is command]
    cwd = command_cwd(command, recording.get("root"), min(direct_offsets) if direct_offsets else None)
    repo = resolver.repo(cwd) or resolver.repo(fallback)
    rows = []
    for (offset, is_direct, text), candidate in zip(contexts, assignments):
        if not candidate and not is_direct:
            continue
        message = normalize_subject(extract_message(text, offset))
        recorded, basis, subject, at = candidate or (None, None, None, 0)
        subject = normalize_subject(subject) or message
        full = resolver.full(repo, recorded) if repo and recorded else None
        if full and len(recorded) < 40:
            basis += "+repo"
        confidence = "recorded" if basis else "unresolved"
        if not recorded and repo and subject and not failed:
            full, matched, basis = _subject_match(resolver, repo, subject, event.get("timestamp"))
            subject = matched or subject
            confidence = "inferred" if full else "unresolved"
        rows.append({
            "recording_id": event.get("recording_id"), "event_id": event.get("event_id"),
            "session": recording.get("session"), "harness": recording.get("harness"),
            "timestamp": event.get("timestamp"), "repo_id": resolver.identity(repo), "repo": repo,
            "command_cwd": cwd, "sha": full or (recorded if recorded and len(recorded) == 40 else None),
            "recorded_sha": recorded, "subject": subject, "command_subject": message,
            "command_offset": offset, "basis": basis or "unresolved", "confidence": confidence,
            "failed": failed, "command": text[:2000],
            "output_excerpt": output[max(0, at - 200):at + 300][:500] if output and candidate else None,
            "ref": event["ref"], "result_ref": event.get("result_ref")})
    return rows


def witnesses(recordings, resolver, cache, fallback=None):
    """Every witness in scanned recordings, before any event filter is applied."""
    rows = []
    for recording in recordings:
        events = recording["events"]
        by_ref = {(e["ref"]["offset"], e["ref"].get("item", 0)): e for e in events}
        for event in events:
            if event["kind"] in ("tool_call", "command"):
                rows.extend(_event_witnesses(event, recording, by_ref, cache, resolver, fallback))
    return rows
