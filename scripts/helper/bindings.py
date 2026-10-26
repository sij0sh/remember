"""Parse explicit SQL bindings: declared column types and named parameters."""
from .engine import IDENTIFIER


def parse_types(definitions, default_view=None):
    """[(view, column, TYPE)] from [VIEW.]COLUMN=TYPE; a missing view uses the default."""
    found = []
    for definition in definitions or ():
        left, sep, kind = str(definition).partition("=")
        if not sep or not left or not kind:
            raise ValueError("Use --type [VIEW.]COLUMN=TYPE, such as rows.exit_code=BIGINT")
        view, dot, column = left.rpartition(".")
        if not dot:
            view, column = default_view, left
        if not view or not IDENTIFIER.fullmatch(column):
            raise ValueError(f"Invalid --type target: {left!r}")
        found.append((view, column, kind.upper()))
    return found


def types_for(definitions, view, alias=None, first=False):
    """Declared types for one view; accepts its name, tN alias, or rows for the first view."""
    names = {name for name in (view, alias, "rows" if first else None) if name}
    return {column: kind for target, column, kind in definitions if target in names}


def parse_params(definitions):
    params = {}
    for definition in definitions or ():
        name, sep, value = str(definition).partition("=")
        if not sep or not IDENTIFIER.fullmatch(name):
            raise ValueError("Use --param NAME=VALUE; NAME must be an identifier")
        params[name] = value
    return params
