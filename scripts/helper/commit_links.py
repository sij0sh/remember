"""Commit witnesses derived from already-scanned session events; nothing is rescanned.

A commit-producing command is paired with its recorded result: the new SHA comes
from ``[branch sha]``, ``commit <sha>``, a one-line ``git log`` entry, or ``HEAD
is now at``. Quiet commits with no recorded SHA may be resolved by subject against
the command's local history; those witnesses are marked ``inferred``, and a subject
shared by several nearby commits stays ``ambiguous_subject``. Commands whose SHA
cannot be recovered stay as ``unresolved`` witnesses with a null SHA.
"""
import datetime as dt
import json
import re

from .git_commands import (assign_shas, command_cwd, commit_contexts, extract_message,
                           normalize_subject, output_shas)
from .profiles import pointer

GRAIN = "one event-to-commit witness"
# Subject matches farther than this from the recorded command are not candidates.
WINDOW_SECONDS = 24 * 3600
COLUMNS = {"witness_id": "VARCHAR", "operation_id": "VARCHAR", "relation": "VARCHAR",
           "recording_id": "VARCHAR", "event_id": "VARCHAR", "session": "VARCHAR", "harness": "VARCHAR",
           "lane": "VARCHAR", "branch": "VARCHAR", "order": "BIGINT", "request_ref": "JSON",
           "timestamp": "VARCHAR", "repo_id": "VARCHAR", "repo": "VARCHAR", "command_cwd": "VARCHAR",
           "sha": "VARCHAR", "recorded_sha": "VARCHAR", "subject": "VARCHAR", "command_subject": "VARCHAR",
           "command_offset": "BIGINT", "basis": "VARCHAR", "confidence": "VARCHAR", "failed": "BOOLEAN",
           "observation": "VARCHAR", "resolution": "VARCHAR",
           "object_resolved": "VARCHAR", "reachable_from_head": "VARCHAR",
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
    if stamp is None:
        # Without a usable recorded time no subject match is near this command.
        return None, None, None
    rows = [row for row in rows if abs(row[2] - stamp) <= WINDOW_SECONDS]
    if len(rows) == 1:
        return rows[0][0], rows[0][1], "repo_subject"
    return None, None, "ambiguous_subject" if rows else None

def _shas(value):
    """Every full SHA named by a structured commit result, in payload order."""
    found = []
    def walk(item):
        if isinstance(item, dict):
            for key, value in item.items():
                if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}", value):
                    found.append(value.lower())
                else:
                    walk(value)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, str):
            found.extend(match.lower() for match in re.findall(r"\b[0-9a-fA-F]{40}\b", item))
    try:
        walk(json.loads(value) if isinstance(value, str) and value.lstrip().startswith(("{", "[")) else value)
    except ValueError:
        walk(value)
    return list(dict.fromkeys(found))


def _result_details(result_event, cache):
    """The recorded tool-result details object, if the harness kept one."""
    if not result_event:
        return {}
    try:
        record = cache.get(result_event["ref"])
        for path in ("/message/details", "/details", result_event.get("payload_pointer", "")):
            value = pointer(record, path)
            if isinstance(value, dict):
                return value
        return {}
    except (KeyError, IndexError, TypeError, ValueError, OSError):
        return {}


def _structured_witnesses(event, recording, by_ref, cache, resolver, fallback):
    """Commit witnesses read from a structured commit-plan/record result; failures never establish a commit."""
    if event.get("tool") not in ("git_commit_plan", "git_record"):
        return []
    result_ref = event.get("result_ref") or {}
    result_event = by_ref.get((result_ref.get("offset"), result_ref.get("item", 0)))
    # A missing result is unknown; never infer a commit from the submitted arguments.
    output = cache.payload(result_event) if result_event else ""
    details = _result_details(result_event, cache)
    try:
        payload = json.loads(output) if isinstance(output, str) and output.lstrip().startswith(("{", "[")) else None
    except ValueError:
        payload = None
    # A commit result may also echo its base or parent; only adapter-supported
    # commit-producing fields are new-commit evidence. Never scan the payload for SHAs.
    # Recorded Pi commit tools report new commits under details.commits; only those
    # arrays (and explicit top-level commit fields) name new commits, never base/parent.
    named = [details.get("sha")] if isinstance(details.get("sha"), str) else []
    subjects = {}
    details_commits = details.get("commits")
    if isinstance(details_commits, list):
        for item in details_commits:
            if isinstance(item, dict) and isinstance(item.get("sha"), str):
                named.append(item["sha"])
                if item.get("subject") or item.get("message"):
                    subjects[item["sha"].lower()] = item.get("subject") or item.get("message")
    if not named and isinstance(payload, dict):
        named = [payload.get(key) for key in ("sha", "commit", "commitSha", "new_sha", "head")
                 if isinstance(payload.get(key), str)]
        commits = payload.get("commits")
        if isinstance(commits, list):
            for item in commits:
                if isinstance(item, dict) and isinstance(item.get("sha"), str):
                    named.append(item["sha"])
                    if item.get("subject") or item.get("message"):
                        subjects[item["sha"].lower()] = item.get("subject") or item.get("message")
    shas = list(dict.fromkeys(named))
    failed = (event.get("outcome") == "failed" or bool(result_event and result_event.get("outcome") == "failed"))
    missing = result_event is None
    repo = resolver.repo(event.get("cwd") or recording.get("root")) or resolver.repo(fallback)
    subject = None
    if isinstance(payload, dict):
        subject = payload.get("subject") or payload.get("message")
        commits = payload.get("commits")
        if not subject and isinstance(commits, list) and commits and isinstance(commits[0], dict):
            subject = commits[0].get("subject") or commits[0].get("message")
    if not subject and isinstance(details, dict):
        subject = details.get("subject") or details.get("message")
        if not subject and isinstance(details_commits, list) and details_commits and \
                isinstance(details_commits[0], dict):
            subject = details_commits[0].get("subject") or details_commits[0].get("message")
    rows = []
    for index, recorded in enumerate(shas or [None]):
        # Without Git, a recorded full SHA remains recorded evidence; only prefix resolution needs a repo.
        if failed:
            full = None
        elif repo and recorded:
            full = resolver.full(repo, recorded)
        else:
            full = recorded if recorded and len(recorded) == 40 else None
        rows.append({
            "witness_id": f"{event.get('recording_id')}#{event.get('event_id')}#structured:{index}",
            "operation_id": f"{event.get('recording_id')}#call:{event.get('event_id')}",
            "relation": "commit",
            "recording_id": event.get("recording_id"), "event_id": event.get("event_id"),
            "session": recording.get("session"), "harness": recording.get("harness"),
            "lane": event.get("lane"), "branch": event.get("branch"), "order": event.get("order"),
            "request_ref": event.get("request_ref"),
            "timestamp": event.get("timestamp"), "repo_id": resolver.identity(repo), "repo": repo,
            "command_cwd": event.get("cwd") or recording.get("root"), "sha": full,
            "recorded_sha": recorded,
            "subject": subjects.get((recorded or "").lower()) or subject,
            "command_subject": None, "command_offset": None,
            "basis": ("structured_commit_record" if event.get("tool") == "git_record"
                      else "structured_commit_result") + ("+repo" if repo and full else ""),
            "confidence": "recorded" if full and not failed else "unresolved",
            "observation": ("direct_result" if full and not failed else "failed_result" if failed
                            else "missing_result" if missing else "unsupported_result"),
            "resolution": ("failed" if failed else "missing_result" if missing else
                           "git_unavailable" if recorded and not repo else
                           "recorded_full_unverified" if recorded and len(recorded) == 40 else
                           "prefix_resolved" if full else "no_sha_in_supported_field" if recorded is None
                           else "unresolved_sha"),
            "failed": bool(failed), "command": f"structured {event.get('tool')} result",
            "output_excerpt": (output or "")[:500] or None,
            "ref": event["ref"], "result_ref": event.get("result_ref")})
    return rows


def _event_witnesses(event, recording, by_ref, cache, resolver, fallback):
    if event.get("tool") in ("git_commit_plan", "git_record"):
        return _structured_witnesses(event, recording, by_ref, cache, resolver, fallback)
    command = event.get("command") or cache.payload(event)
    contexts = commit_contexts(command)
    if not contexts:
        return []
    result_ref = event.get("result_ref") or {}
    result_event = by_ref.get((result_ref.get("offset"), result_ref.get("item", 0)))
    # A missing result is unknown; never infer a commit from the submitted arguments.
    output = cache.payload(result_event) if result_event else ""
    missing = result_event is None
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
        inferred = False
        if not recorded and repo and subject and not failed and not missing:
            full, matched, basis = _subject_match(resolver, repo, subject, event.get("timestamp"))
            subject = matched or subject
            inferred = bool(full)
            confidence = "inferred" if full else "unresolved"
        rows.append({
            "witness_id": f"{event.get('recording_id')}#{event.get('event_id')}#{offset}",
            "operation_id": f"{event.get('recording_id')}#call:{event.get('event_id')}",
            "relation": "commit",
            "recording_id": event.get("recording_id"), "event_id": event.get("event_id"),
            "session": recording.get("session"), "harness": recording.get("harness"),
            "lane": event.get("lane"), "branch": event.get("branch"), "order": event.get("order"),
            "request_ref": event.get("request_ref"),
            "timestamp": event.get("timestamp"), "repo_id": resolver.identity(repo), "repo": repo,
            "command_cwd": cwd, "sha": full or (recorded if recorded and len(recorded) == 40 else None),
            "recorded_sha": recorded, "subject": subject, "command_subject": message,
            "command_offset": offset, "basis": basis or "unresolved", "confidence": confidence,
            "observation": ("direct_result" if basis and not inferred and not failed else
                            "direct_result_in_failed_command" if basis and failed else
                            "subject_inference" if inferred else "missing_result" if missing else "none"),
            "resolution": ("failed" if failed else "missing_result" if missing else
                           "recorded_full_unverified" if recorded and len(recorded) == 40 else
                           "repo_subject_inference" if inferred else
                           "prefix_resolved" if full else "unresolved"),
            "failed": failed, "command": text[:2000],
            "output_excerpt": output[max(0, at - 200):at + 300][:500] if output and candidate else None,
            "ref": event["ref"], "result_ref": event.get("result_ref")})
    return rows


def witnesses(recordings, resolver, cache, fallback=None):
    """Every witness in scanned recordings, before any event filter is applied.

    A recorded SHA, its repository object resolution and HEAD reachability stay separate facts."""
    rows = []
    for recording in recordings:
        events = recording["events"]
        by_ref = {(e["ref"]["offset"], e["ref"].get("item", 0)): e for e in events}
        for event in events:
            if event["kind"] in ("tool_call", "command"):
                rows.extend(_event_witnesses(event, recording, by_ref, cache, resolver, fallback))
    for row in rows:
        row["object_resolved"], row["reachable_from_head"] = resolver.verify(row.get("repo"), row.get("sha"))
    return rows
