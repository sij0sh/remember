"""One delivery component: v2 paging and byte fitting. Nothing is persisted.

Later pages recompute through typed continuations whose signature rejects changed inputs.
Evidence coverage and output paging are independent: paging and byte budgets truncate
delivery, only coverage gaps make a result partial.
"""
import copy
import sys
from .envelope import SCHEMA_VERSION
from .graph_store import canonical
from .graph_output import check_fields, project_rows, render_table

# Room for the continuation token itself when sizing a budget for an oversized row.
NEXT_MARGIN = 2048


def select(rows, args, fields, transform=None):
    """Validate --fields, take the requested page, then project it."""
    check_fields(fields, rows)
    page = rows[args.offset:args.offset + args.limit]
    return project_rows([transform(r) for r in page] if transform else page, fields)


def fit(output, finish, size, max_bytes):
    """Drop rows from the end until the rendering fits, so ranking and --offset paging hold. True if it fits."""
    while output["results"] and size() > max_bytes:
        output["results"].pop()
        finish()
    return size() <= max_bytes


def render(output, args, fields):
    if getattr(args, "format", None) == "table":
        return render_table(output, fields).encode("utf-8") + b"\n"
    return canonical(output) + b"\n"


def page(document, args, continuation=None):
    """Common v2 result; returns (output, bytes). The caller owns source retention: nothing is persisted here."""
    output = copy.deepcopy(document)
    rows = output.pop("results", [])
    fields = args.fields
    total = len(rows)
    output.setdefault("status", "ok")
    output["schema_version"] = SCHEMA_VERSION
    output.setdefault("scope", {}).setdefault("retention", "transient")
    selected = select(rows, args, fields)
    selected_raw = rows[args.offset:args.offset + args.limit]
    output["results"] = list(selected)  # fit() pops from this list; selected stays whole.
    more = bool(output.pop("more_rows", False))
    original_next = output.get("next") or []
    if not isinstance(original_next, list):
        original_next = [original_next]

    def finish():
        following = args.offset + len(output["results"])
        output["delivery"] = {"offset": args.offset, "returned": len(output["results"]),
                              "complete": following >= total and not more,
                              "total": None if more else total}
        output["next"] = list(original_next)
        if not output["delivery"]["complete"] and continuation:
            # Blocking coverage recovery ranks before paging; paging before optional expansion.
            at = sum(1 for item in original_next if isinstance(item, dict) and item.get("effect") == "coverage")
            # A row too large for the budget: ask for enough bytes to deliver it in one step.
            need = (len(render({**output, "results": selected[:1]}, args, fields)) + NEXT_MARGIN
                    if not output["results"] and selected else None)
            output["next"].insert(at, continuation(following, not output["results"], need))
        if not output["delivery"]["complete"]:
            output["results_truncated"] = "More computed rows exist; follow next. Coverage is reported separately."
        if isinstance(output.get("views"), dict):
            # Per-page counts from the selected rows (before projection), so --fields cannot hide them.
            shown = selected_raw[:len(output["results"])]
            for name, view in output["views"].items():
                view["delivered"] = sum(name in (row.get("categories") or ()) for row in shown)
                view["omitted"] = max(0, view.get("eligible", 0) - view["delivered"])
    finish()
    def size():
        return len(render(output, args, fields))
    if not fit(output, finish, size, args.max_bytes):
        # Keep the operation, coverage state and executable continuation rather than an oversized envelope.
        output["coverage"] = {"summary_omitted": True, "state": output["status"]}
        output["scope"] = {"retention": output["scope"].get("retention", "transient")}
        for key in ("target", "steps", "gaps", "snapshots", "plan", "notes"):
            output.pop(key, None)
    if size() > args.max_bytes and not output["results"] and not output["delivery"]["complete"]:
        # Budget recovery: keep category states and the one ref that raises --max-bytes; drop the rest.
        kept = [item for item in output["next"] if isinstance(item, dict) and item.get("effect") == "delivery"]
        if kept:
            output["next"] = kept[:1]
            output["next_omitted"] = "coverage/expansion refs dropped for byte budget"
            output.pop("results_truncated", None)
    if size() > args.max_bytes:
        # Never silently discard a selected evidence payload or emit malformed/truncated JSON.
        raise ValueError("Result metadata/continuation exceeds --max-bytes; increase the output budget")
    return output, render(output, args, fields)


def write(raw):
    sys.stdout.flush()
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def deliver(document, args, continuation=None):
    """Render and write one paged result."""
    write(page(document, args, continuation)[1])
