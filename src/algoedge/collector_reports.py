"""Read-only access to the Phase 8 collector's evidence files.

Only lists and serves ``*.jsonl`` files sitting directly in one fixed
directory (research/phase8/evidence/campaign_status). The browser only ever
sends a bare filename; it is matched against a strict pattern and then must
resolve to a regular file inside that directory, so no path (``..``,
separators, absolute paths, symlinks out of the directory) can reach any
other file. Files are served byte-for-byte - never parsed or rewritten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

COLLECTOR_EVIDENCE_DIR = (
    Path(__file__).resolve().parents[2] / "research" / "phase8" / "evidence" / "campaign_status"
)

# Collector files look like status_20261005T091500+0530_0123456789ab.jsonl.
_FILENAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\.jsonl")


class InvalidReportName(ValueError):
    """The requested name is not an acceptable collector report filename."""


@dataclass(frozen=True)
class CollectorReport:
    name: str
    size_bytes: int
    modified_at: datetime


def _is_valid_name(name: str) -> bool:
    return bool(_FILENAME_PATTERN.fullmatch(name)) and ".." not in name


def _resolve_inside(directory: Path, name: str) -> Path | None:
    """The regular file ``name`` inside ``directory``, or None if it doesn't
    exist or resolves anywhere else (e.g. a symlink pointing out)."""
    root = directory.resolve()
    candidate = (root / name).resolve()
    if candidate.parent != root or not candidate.is_file():
        return None
    return candidate


def list_collector_reports(directory: Path = COLLECTOR_EVIDENCE_DIR) -> list[CollectorReport]:
    """Newest first. A missing directory is simply "no reports"."""
    if not directory.is_dir():
        return []
    reports = []
    for entry in directory.iterdir():
        if not _is_valid_name(entry.name):
            continue
        path = _resolve_inside(directory, entry.name)
        if path is None:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue  # deleted between listing and stat
        reports.append(
            CollectorReport(
                name=entry.name,
                size_bytes=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            )
        )
    reports.sort(key=lambda report: (report.modified_at, report.name), reverse=True)
    return reports


def resolve_collector_report(name: str, directory: Path = COLLECTOR_EVIDENCE_DIR) -> Path | None:
    """The path to serve for ``name``. Raises InvalidReportName for anything
    that isn't a plain ``.jsonl`` filename; returns None if it doesn't exist."""
    if not _is_valid_name(name):
        raise InvalidReportName(name)
    return _resolve_inside(directory, name)
