"""Explicit option objects shared by CLIs, discovery, and the reader."""
from dataclasses import dataclass

# Defaults applied when a limit is omitted. A project filter reads every header cheaply, so
# discovery reaches the whole archive and a typical project's recordings are all scanned; text grows to the selected recordings' size up to a cap.
CANDIDATES_DEFAULT = 1000
FILES_DEFAULT = 8
FILES_PROJECT = 25
CANDIDATES_PROJECT = 20000
TEXT_BYTES_DEFAULT = 8 * 1024 * 1024
TEXT_BYTES_AUTO_CAP = 32 * 1024 * 1024


@dataclass(frozen=True)
class DiscoveryOptions:
    files: tuple[str, ...] = ()
    harness: str = "auto"
    project: str | None = None
    max_files: int = FILES_DEFAULT
    max_candidates: int = CANDIDATES_DEFAULT
    max_record_bytes: int = 2 * 1024 * 1024
    # Earlier roots of the same project (--root-map OLD=NEW): read beside the project filter.
    also_roots: tuple[str, ...] = ()

    @classmethod
    def from_args(cls, args):
        return cls(files=tuple(args.files), harness=args.harness, project=args.project,
                   max_files=args.max_files, max_candidates=args.max_candidates,
                   max_record_bytes=args.max_record_bytes,
                   also_roots=tuple(m.partition("=")[0] for m in getattr(args, "root_map", None) or ()))


@dataclass(frozen=True)
class ScanOptions:
    max_record_bytes: int = 2 * 1024 * 1024
    max_scan_bytes: int = 64 * 1024 * 1024
    max_events: int = 25000
    max_text_bytes: int = TEXT_BYTES_DEFAULT
    preview_chars: int = 300
    text: tuple[str, ...] = ()
    harness: str = "auto"

    @classmethod
    def from_args(cls, args, max_text_bytes=None):
        return cls(max_record_bytes=args.max_record_bytes, max_scan_bytes=args.max_scan_bytes,
                   max_events=args.max_events,
                   max_text_bytes=max_text_bytes or args.max_text_bytes or TEXT_BYTES_DEFAULT,
                   preview_chars=args.preview_chars, text=tuple(args.text), harness=args.harness)
