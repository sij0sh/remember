"""Compact presentation shared by retained graph reports and transient history."""
import json

COLUMNS = ('kind', 'role', 'path', 'line', 'qualname', 'name', 'distance', 'via',
           'consumers', 'test_consumers', 'dependencies', 'callers', 'caller_files',
           'lines', 'size', 'count', 'state', 'reason', 'files')


def project_rows(rows, fields):
    return [{k: row[k] for k in fields if k in row} for row in rows] if fields else list(rows)


def render_table(output, fields=None):
    rows = output['results']
    present = {k for row in rows for k in row}
    columns = fields or [k for k in COLUMNS if k in present] or sorted(present)
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
