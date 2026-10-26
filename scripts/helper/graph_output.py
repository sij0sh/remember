"""Compact presentation shared by retained graph reports and transient history."""
import json

COLUMNS = ('kind', 'role', 'path', 'line', 'qualname', 'name', 'distance', 'via',
           'consumers', 'test_consumers', 'dependencies', 'callers', 'caller_files',
           'lines', 'size', 'count', 'state', 'reason', 'files')


# Defaults and identifiers already carried by `ref`; --verbose keeps them.
QUIET = {"id": None, "key": None, "digest": None, "state": "read", "parse_state": "parsed",
         "is_test": False, "public_candidate": False}


def compact_row(row):
    """A delivered row without identifiers `ref` repeats or values that are the default."""
    out = {}
    for key, value in row.items():
        if key in QUIET and (QUIET[key] is None or value == QUIET[key]):
            continue
        if key == "witness" and isinstance(value, list):
            # The edge ref names both endpoints; keep only what to expand.
            value = [{k: v for k, v in step.items() if k in ("predicate", "edge")
                      or (k == "resolution" and v != "resolved")} for step in value]
        elif isinstance(value, dict) and value.get("kind") in ("file", "symbol") and "ref" in value:
            value = compact_row(value)
        out[key] = value
    return out


def project_rows(rows, fields):
    return [{k: row[k] for k in fields if k in row} for row in rows] if fields else list(rows)


def render_table(output, fields=None):
    rows = output['results']
    # First-appearance order: SQL rows keep their SELECT order, aliases and aggregates included.
    present = list(dict.fromkeys(k for row in rows for k in row))
    if output.get('operation') == 'query':
        columns = fields or present
    else:
        columns = fields or [k for k in COLUMNS if k in present] or present
    def cell(value):
        text = json.dumps(value, ensure_ascii=True, separators=(',', ':')) if isinstance(value, (dict, list)) else str(value) if value is not None else ''
        text = text.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
        return text[:77] + '...' if len(text) > 80 else text
    values = [[cell(row.get(k)) for k in columns] for row in rows]
    widths = [max(len(k), *(len(row[i]) for row in values)) if values else len(k) for i, k in enumerate(columns)]
    delivery = output.get('delivery', {})
    lines = [f"{output.get('status', 'ok')} | {output.get('operation', 'report')} | "
             f"{len(rows)} of {delivery.get('total', '?')} rows | {output.get('report', 'transient')}"]
    scope = output.get('scope', {})
    for key in ('path_result', 'assessment', 'unresolved_mentions'):
        if key in scope:
            lines.append(f'{key}: {json.dumps(scope[key], ensure_ascii=True)}')
    primer = output.get('primer')
    if isinstance(primer, str):
        lines.append('primer: ' + primer)
    elif isinstance(primer, list):
        lines.extend(primer)
    if output.get('gaps'):
        lines.append('gaps: ' + json.dumps(output['gaps'], ensure_ascii=True))
    if output.get('results_truncated'):
        lines.append('TRUNCATED: ' + output['results_truncated'])
    if columns:
        lines.append('  '.join(k.ljust(w) for k, w in zip(columns, widths)))
        lines.extend('  '.join(v.ljust(w) for v, w in zip(row, widths)).rstrip() for row in values)
    if output.get('next'):
        lines.append('next: ' + json.dumps(output['next'], ensure_ascii=True))
    return '\n'.join(lines)
