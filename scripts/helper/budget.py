"""Invocation-wide cooperative deadlines, inherited by scans and Git subprocesses."""
import contextvars
import time

CURRENT = contextvars.ContextVar("remember_deadline", default=None)
POLICIES = {
    "standard": {"seconds": 30, "max_files": 20000, "max_total_bytes": 32 * 1024 * 1024,
                 "max_recordings": 25, "max_candidates": 20000, "max_scan_bytes": 64 * 1024 * 1024,
                 "max_events": 25000, "max_commits": 200, "max_query_rows": 100000},
    "deep": {"seconds": 120, "max_files": 50000, "max_total_bytes": 128 * 1024 * 1024,
             "max_recordings": 100, "max_candidates": 50000, "max_scan_bytes": 256 * 1024 * 1024,
             "max_events": 100000, "max_commits": 2000, "max_query_rows": 200000},
}


class Deadline:
    def __init__(self, seconds):
        self.end = time.monotonic() + seconds

    def remaining(self):
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise ValueError("Invocation deadline exceeded; narrow the request or use --budget deep")
        return remaining

    def check(self):
        self.remaining()

    def __enter__(self):
        self.token = CURRENT.set(self)
        return self

    def __exit__(self, *exc):
        CURRENT.reset(self.token)


def check():
    current = CURRENT.get()
    if current is not None:
        current.check()


def timeout(seconds):
    current = CURRENT.get()
    return min(seconds, current.remaining()) if current is not None else seconds
