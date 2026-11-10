"""`show <cite>`: one recorded event, its paired tool call/result and its turn's human request."""
from types import SimpleNamespace

from . import cite
from .discovery import candidate_files
from .envelope import incomplete
from .options import DiscoveryOptions, ScanOptions
from .projection import clean
from .recording_scan import scan_group, text_budget

MAX_RECORD_BYTES = 2 * 1024 * 1024


def expand(value):
    ref = cite.ref_argument(value, MAX_RECORD_BYTES)
    # The ref fixes the file; no project filter, one recording.
    args = SimpleNamespace(files=[ref["file"]], harness="auto", project=None, max_files=1, max_candidates=1,
                           max_record_bytes=MAX_RECORD_BYTES, max_scan_bytes=64 * 1024 * 1024, max_events=25000,
                           max_text_bytes=None, preview_chars=300, text=[], root_map=[])
    scope = {}
    events = []
    for _harness, files, gscope in candidate_files(DiscoveryOptions.from_args(args), scope):
        for recording in scan_group(files, ScanOptions.from_args(args, text_budget(args, files, gscope)), gscope):
            events.extend(recording["events"])
        scope.update({k: v for k, v in gscope.items() if k != "snapshot"})
    key = (ref["offset"], ref.get("item", 0))
    target = next((e for e in events if (e["ref"]["offset"], e["ref"]["item"]) == key), None)
    if target is None:
        raise ValueError("Referenced event not found within scan bounds")
    if target["ref"]["sha256"] != ref["sha256"]:
        raise ValueError("Source changed at this ref; search again")
    keys = {(r["offset"], r["item"]) for r in (target["ref"], target.get("call_ref"), target.get("result_ref")) if r}
    # The human request that opened this turn: why the exchange happened.
    request_ref = target.get("request_ref")
    request = None
    if isinstance(request_ref, dict):
        wanted = (request_ref.get("offset"), request_ref.get("item", 0))
        request = next((e for e in events if (e["ref"]["offset"], e["ref"].get("item", 0)) == wanted
                        and e["kind"] == "user_message"), None)
    if request is None:
        request = next((e for e in events if target.get("turn") and e.get("turn") == target["turn"]
                        and e["kind"] == "user_message" and e.get("request_origin") == "human"), None)
    if request is not None:
        keys.add((request["ref"]["offset"], request["ref"]["item"]))
    rows = [{**clean(e, None), "selected": True, "cite": cite.token(e["ref"])} if e is target
            else {**clean(e, SimpleNamespace(preview_chars=300)), "cite": cite.token(e["ref"])}
            for e in events if (e["ref"]["offset"], e["ref"]["item"]) in keys]
    reasons = incomplete({"scope": scope}, scope)
    return {"operation": "show", "status": "partial" if reasons else "ok", "results": rows,
            "scope": {**scope, "relationship_basis": "turn request + exact recorded pairing; absent partner remains unknown"},
            "coverage": {"session": {"complete": not reasons, "reasons": reasons}}, "next": []}
