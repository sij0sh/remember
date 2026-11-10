"""Recorded path prefixes rewritten OLD=NEW, for recordings made under another checkout."""
import os


def parse_root_map(values):
    """[(old, new)] normalized, longest OLD first so a nested mapping wins."""
    pairs = []
    for value in values or ():
        old, _, new = value.partition("=")
        pairs.append((os.path.normpath(os.path.expanduser(old)), os.path.normpath(os.path.expanduser(new))))
    return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)


def remap(path, pairs):
    """path with the first matching OLD prefix replaced by NEW; (path, mapped)."""
    if not path:
        return path, False
    normal = os.path.normpath(path)
    for old, new in pairs:
        if normal == old or normal.startswith(old.rstrip(os.sep) + os.sep):
            return new + normal[len(old):], True
    return path, False
