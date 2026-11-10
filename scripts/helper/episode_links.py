"""Episodes (one human request and the events it governs) and bounded links between them (D136-D141).

An episode reuses the reader's `request_ref`; follow-up acknowledgements on the same lane stay in
the episode they answer. Links are candidate relations found only through shared features
(paths, error families, SHAs) or explicit recorded structure, ordered earlier -> later. Strength
says how much the link rests on: `explicit` (named SHA, parent session, adjacent reply),
`supported` (shared error family and path, or same-exchange work), `weak` (shared path only).
No link is causal proof. Nothing is persisted.
"""
import hashlib
import json
import re

from . import error_signatures, evidence_candidates, message_segments
from .budget import check
from .graph_store import canonical_path
from .projection import human
from .target_history import CORRECTION

EPISODE_GRAIN = "one human request and the events it governs in one recording"
LINK_GRAIN = "one candidate relation between two episodes, or an episode and its work"
EPISODE_COLUMNS = {"episode_id": "VARCHAR", "recording_id": "VARCHAR", "recordings": "JSON",
                   "independent_session": "VARCHAR", "lane": "VARCHAR", "request_ref": "JSON",
                   "request_event_id": "VARCHAR", "timestamp": "VARCHAR", "start_order": "BIGINT",
                   "end_order": "BIGINT", "events": "BIGINT", "operations": "BIGINT", "boundary": "VARCHAR"}
LINK_COLUMNS = {"link_id": "VARCHAR", "source": "VARCHAR", "target": "VARCHAR", "relation": "VARCHAR",
                "strength": "VARCHAR", "basis": "VARCHAR", "matched": "JSON", "evidence_refs": "JSON",
                "ordering_basis": "VARCHAR", "response": "VARCHAR", "alternatives": "JSON"}
RELATIONS = ("explicit_followup", "possible_regression", "possible_same_problem", "proposal_response",
             "possible_revision", "implementation_link", "related_context")
PER_RELATION = 5
EVIDENCE = 5
ALTERNATIVES = 3
FIX_CLAIM = re.compile(r"\b(fix(ed|es)?|should (now )?work|resolved|patched)\b", re.I)
# Kept in step with investigate.CHOICE / ACK; duplicated to avoid an import cycle.
CHOICE = re.compile(r"\b(decided|decision|we(?:'ll| will) (?:go with|use)|go with|chose|choose|instead of|"
                    r"rather than|trade-?off|opted|in favou?r of|prefer(?:red)? (?:to|over))\b", re.I)
ACK = re.compile(r"^\W*(ok(ay)?|yes|yep|yeah|y|no|nope|thanks?( you)?|ty|go( ahead)?|continue|proceed|sure|do it|"
                 r"lgtm|great|nice|done|perfect|cool|good)\W*$", re.I)
ACCEPT = re.compile(r"^\W*(ok(ay)?|yes|yep|yeah|y|go( ahead)?|continue|proceed|sure|do it|lgtm|great|nice|perfect|"
                    r"cool|good)\b|\bgo with\b", re.I)


def _key(ref):
    return (ref.get("offset"), ref.get("item", 0)) if isinstance(ref, dict) else None


def _id(prefix, *parts):
    raw = json.dumps(parts, separators=(",", ":"), default=str)
    return f"{prefix}:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _locator(ref):
    return {k: ref.get(k) for k in ("file", "offset", "item") if ref.get(k) is not None} if ref else None


def _request(event):
    """A request opens an episode; short acknowledgements answer the current one."""
    if not human(event):
        return False
    text = message_segments.prose(event)
    return len(text) >= 12 and not ACK.match(text) and not (text.startswith("/") and len(text.split()) <= 3)


class Episode:
    def __init__(self, recording, request):
        self.recording, self.request = recording, request
        self.id = _id("ep", canonical_path(recording["file"]), request.get("event_id") or _key(request["ref"]))
        self.lane, self.events, self.ops, self.shas = request.get("lane"), [], [], set()
        self.paths, self.families, self.failures, self.fixes = set(), set(), [], []
        self.recordings = [recording["file"]]

    def row(self, boundary):
        orders = [e.get("order") or 0 for e in self.events]
        return {"episode_id": self.id, "recording_id": self.recording["file"], "recordings": self.recordings,
                "independent_session": self.request.get("independent_session"), "lane": self.lane,
                "request_ref": self.request["ref"], "request_event_id": self.request.get("event_id"),
                "timestamp": self.request.get("timestamp"), "start_order": min(orders, default=0),
                "end_order": max(orders, default=0), "events": len(self.events), "operations": len(self.ops),
                "boundary": boundary}

    @property
    def order(self):
        return (self.request.get("timestamp") or "", self.request.get("order") or 0)


def _episodes(recordings, operations, witnesses, root):
    """[Episode] with features, plus {(recording, request key): Episode}."""
    out, by_request = [], {}
    for recording in recordings:
        current = {}
        for event in recording["events"]:
            if event.get("kind") == "user_message" and _request(event):
                ep = Episode(recording, event)
                out.append(ep)
                current[event.get("lane")] = ep
                by_request[(recording["file"], _key(event["ref"]))] = ep
            elif event.get("kind") == "user_message" and human(event) and event.get("lane") in current:
                by_request[(recording["file"], _key(event["ref"]))] = current[event.get("lane")]
        for event in recording["events"]:
            ep = by_request.get((recording["file"], _key(event.get("request_ref"))))
            if ep is None:
                continue
            ep.events.append(event)
            if event.get("kind") in ("user_message", "assistant_message"):
                text = message_segments.prose(event)
                for kind, value in evidence_candidates.features(message_segments.visible_lines(event.get("text")), root):
                    if kind == "path":
                        ep.paths.add(value)
                    elif kind == "error_family":
                        ep.families.add(value)
                    elif kind == "sha":
                        ep.shas.add(("named", value))
                if FIX_CLAIM.search(text):
                    ep.fixes.append(("claim", event.get("order") or 0))
    events = {(r["file"], _key(e["ref"])): e for r in recordings for e in r["events"]}
    for op in operations:
        ep = by_request.get((op.get("recording_id"), _key(op.get("request_ref"))))
        if ep is None:
            continue
        ep.ops.append(op)
        path = op.get("repo_path") or (op.get("path") if op.get("path_location") == "repository" else None)
        if path:
            ep.paths.add(path)
        if op.get("outcome") == "failed":
            result = events.get((op.get("recording_id"), _key(op.get("result_ref"))))
            for _, family in error_signatures.signature(message_segments.visible_lines((result or {}).get("text"))):
                ep.families.add(family)
                ep.failures.append((family, op.get("order") or 0, op))
        elif op.get("action") == "file_edit" and op.get("outcome") == "succeeded":
            ep.fixes.append(("edit", op.get("order") or 0, path))
    for link in witnesses:
        ep = by_request.get((link.get("recording_id"), _key(link.get("request_ref"))))
        if ep is not None and link.get("sha") and not link.get("failed"):
            ep.shas.add(("commit", link["sha"]))
            ep.fixes.append(("commit", link.get("order") or 0, None))
    return out, by_request


def _dedupe(episodes):
    """Forked or duplicated transcripts: same session, message id and prose -> one episode (D137)."""
    kept, seen = [], {}
    for ep in episodes:
        req = ep.request
        key = (req.get("independent_session"), req.get("message_id") or req.get("external_message_id"),
               hashlib.sha256(message_segments.prose(req).encode()).hexdigest())
        if key[1] and key in seen:
            seen[key].recordings.append(ep.recording["file"])
            continue
        seen[key] = ep
        kept.append(ep)
    return kept


def _fix_attempt(ep, after, paths):
    """D145: a succeeded edit or commit on a shared path, plus fix-claim prose, after ``after``."""
    work = any(f[0] in ("edit", "commit") and f[1] > after and (f[2] is None or f[2] in paths) for f in ep.fixes)
    claim = any(f[0] == "claim" and f[1] > after for f in ep.fixes)
    return work and claim


def _ordering(a, b):
    if a.recording["file"] == b.recording["file"]:
        return "same_lane_order" if a.lane == b.lane else "timestamp_only"
    if a.request.get("independent_session") and a.request.get("independent_session") == b.request.get("independent_session"):
        return "ancestry"
    return "timestamp_only" if a.request.get("timestamp") and b.request.get("timestamp") else "unknown"


class Links:
    def __init__(self):
        self.rows, self.count, self.omitted, self.seen = [], {}, 0, set()

    def add(self, source, target, relation, strength, basis, matched=(), refs=(), ordering=None, **extra):
        # Per-evidence relations (one proposal, one edit) keep each instance; others are one per pair.
        anchor = json.dumps(refs[0], sort_keys=True) if refs and relation in ("proposal_response",) else None
        key = (source, target, relation, anchor)
        if key in self.seen:
            return
        self.seen.add(key)
        bucket = (source, relation)
        if self.count.get(bucket, 0) >= PER_RELATION:
            self.omitted += 1
            return
        self.count[bucket] = self.count.get(bucket, 0) + 1
        self.rows.append({"link_id": _id("link", source, target, relation, anchor), "source": source, "target": target,
                          "relation": relation, "strength": strength, "basis": basis,
                          "matched": sorted(matched)[:3], "evidence_refs": [r for r in refs if r][:EVIDENCE],
                          "ordering_basis": ordering or "unknown", "response": extra.get("response"),
                          "alternatives": extra.get("alternatives") or []})


def _proposals(recordings, by_request, links):
    """proposal_response and implementation_link (D138, D139)."""
    proposals = []
    for recording in recordings:
        pending = {}
        for event in recording["events"]:
            lane = event.get("lane")
            if event.get("kind") == "assistant_message":
                text = message_segments.prose(event)
                if CHOICE.search(text):
                    pending.setdefault(lane, []).append(event)
                    ep = by_request.get((recording["file"], _key(event.get("request_ref"))))
                    if ep is not None:
                        proposals.append((ep, event, {v for k, v in evidence_candidates.features(text) if k == "path"}))
            elif event.get("kind") == "user_message" and human(event) and pending.get(lane):
                text = message_segments.prose(event)
                response = ("rejected" if CORRECTION.search(text[:300]) else
                            "accepted" if ACCEPT.search(text) else "unclear")
                group = pending.pop(lane)
                if response == "accepted" and len(group) > 1 and ACK.match(text):
                    response = "unclear"  # one ack after several proposals adopts none of them (D139)
                target = by_request.get((recording["file"], _key(event["ref"])))
                for proposal in group:
                    source = by_request.get((recording["file"], _key(proposal.get("request_ref"))))
                    if source is None or target is None:
                        continue
                    others = [_locator(p["ref"]) for p in group if p is not proposal][:ALTERNATIVES]
                    links.add(source.id, target.id, "proposal_response", "explicit", "next_human_message",
                              refs=[_locator(proposal["ref"]), _locator(event["ref"])], ordering="same_lane_order",
                              response=response, alternatives=others)
    for ep, event, _ in proposals:
        for op in ep.ops:
            if op.get("action") == "file_edit" and op.get("outcome") == "succeeded" and (op.get("order") or 0) > (event.get("order") or 0):
                links.add(ep.id, op.get("operation_id"), "implementation_link", "supported", "same_exchange",
                          matched=[op.get("repo_path") or op.get("path") or ""],
                          refs=[_locator(event["ref"]), _locator(op.get("call_ref"))], ordering="same_lane_order")
        for kind, sha in sorted(ep.shas):
            if kind == "commit":
                links.add(ep.id, "commit:" + sha, "implementation_link", "supported", "same_exchange",
                          matched=[sha], refs=[_locator(event["ref"])], ordering="same_lane_order")
    return proposals


def build(recordings, operations, witnesses, root=None):
    """(episode rows, link rows, coverage) over loaded recordings."""
    episodes, by_request = _episodes(recordings, operations, witnesses, root)
    episodes = _dedupe(episodes)
    alive = {ep.id for ep in episodes}
    by_request = {k: v for k, v in by_request.items() if v.id in alive}
    links = Links()
    proposals = _proposals(recordings, by_request, links)
    episodes.sort(key=lambda ep: ep.order)
    rank = {ep.id: index for index, ep in enumerate(episodes)}
    # Feature index: candidate pairs share at least one feature.
    index = {}
    for ep in episodes:
        for path in ep.paths:
            index.setdefault(("path", path), []).append(ep)
        for family in ep.families:
            index.setdefault(("family", family), []).append(ep)
    pairs = {}
    for (kind, value), members in index.items():
        check()
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                pair = pairs.setdefault((a.id, b.id), (a, b, set(), set()))
                pair[2 if kind == "path" else 3].add(value)
    shas = {}
    for ep in episodes:
        for kind, sha in ep.shas:
            if kind == "commit":
                shas.setdefault(sha, ep)
    for later in episodes:
        for kind, named in sorted(later.shas):
            if kind != "named":
                continue
            earlier = next((ep for sha, ep in shas.items() if sha.startswith(named) and ep is not later
                            and rank[ep.id] < rank[later.id]), None)
            if earlier is not None:
                links.add(earlier.id, later.id, "explicit_followup", "explicit", "named_commit", matched=[named],
                          refs=[_locator(later.request["ref"])], ordering=_ordering(earlier, later))
    # A recording's parent is named by file (Pi) or session id; its last episode precedes the child's first.
    owner = {r["file"]: r["file"] for r in recordings}
    owner.update({r["session"]: r["file"] for r in recordings if r.get("session")})
    first, last = {}, {}
    for ep in episodes:
        first.setdefault(ep.recording["file"], ep)
        last[ep.recording["file"]] = ep
    for name, ep in first.items():
        parent_ep = last.get(owner.get(ep.recording.get("parent_session")))
        if parent_ep is not None and parent_ep.recording is not ep.recording:
            links.add(parent_ep.id, ep.id, "explicit_followup", "explicit", "parent_session",
                      refs=[_locator(ep.request["ref"])], ordering="ancestry")
    for a, b, paths, families in sorted(pairs.values(), key=lambda p: (rank[p[0].id], rank[p[1].id])):
        earlier, later = (a, b) if rank[a.id] < rank[b.id] else (b, a)
        ordering = _ordering(earlier, later)
        refs = [_locator(earlier.request["ref"]), _locator(later.request["ref"])]
        if families and paths:
            regression = any(fam in families and any(f2 == fam for f2, *_ in later.failures)
                             and _fix_attempt(earlier, order, paths)
                             for fam, order, _ in earlier.failures)
            relation = "possible_regression" if regression else "possible_same_problem"
            links.add(earlier.id, later.id, relation, "supported", "error_family_and_path",
                      matched=sorted(families)[:2] + sorted(paths)[:1], refs=refs, ordering=ordering)
        elif paths:
            links.add(earlier.id, later.id, "related_context", "weak", "shared_path", matched=paths, refs=refs,
                      ordering=ordering)
    by_id = {ep.id: ep for ep in episodes}
    for ep, event, paths in proposals:
        for later in episodes:
            if rank[later.id] <= rank[ep.id] or not paths:
                continue
            for other in later.events:
                if other.get("kind") not in ("user_message", "assistant_message"):
                    continue
                text = message_segments.prose(other)
                shared = paths & {v for k, v in evidence_candidates.features(text) if k == "path"}
                if shared and CHOICE.search(text):
                    links.add(ep.id, later.id, "possible_revision", "weak", "later_choice_shared_path",
                              matched=shared, refs=[_locator(event["ref"]), _locator(other["ref"])],
                              ordering=_ordering(ep, later))
                    break
    rows = []
    for ep in episodes:
        nxt = any(o.recording is ep.recording and o.lane == ep.lane and o.order > ep.order for o in episodes)
        boundary = "closed" if nxt else "open" if ep.recording.get("scan_complete", True) else "truncated"
        rows.append(ep.row(boundary))
    links.rows.sort(key=lambda r: (RELATIONS.index(r["relation"]), rank.get(r["source"], 0), r["link_id"]))
    # Link bounds are selection policy, not a scan gap: omitted links are counted, not marked incomplete.
    coverage = {"episodes": len(rows), "links": len(links.rows),
                "links_omitted": links.omitted, "duplicates_merged": sum(len(ep.recordings) - 1 for ep in episodes),
                "meaning": "candidate relations via shared features; not causal proof"}
    lookup = {"by_id": by_id, "by_request": {key: ep.id for key, ep in by_request.items()}}
    return rows, links.rows, coverage, lookup


def episode_for(lookup, recording_file, request_ref):
    return lookup["by_request"].get((recording_file, _key(request_ref))) if lookup else None


def related(episode_id, link_rows, episodes_by_id, limit=3):
    """<= limit {relation, strength, direction, target}; target = the other episode's full request ref."""
    out = []
    for row in link_rows:
        if episode_id not in (row["source"], row["target"]):
            continue
        other = row["target"] if row["source"] == episode_id else row["source"]
        ep = episodes_by_id.get(other)
        if ep is None or other == episode_id:
            continue
        out.append({"relation": row["relation"], "strength": row["strength"],
                    "direction": "later" if row["source"] == episode_id else "earlier",
                    "target": ep.request["ref"]})
        if len(out) == limit:
            break
    return out
