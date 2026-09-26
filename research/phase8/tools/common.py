"""Shared plumbing for the Phase 8 evidence tools: timestamps, deterministic
JSON, append-only evidence files, credential redaction and a minimal HTTP
client (stdlib only).

Evidence files are created with exclusive-create mode - an existing file is
never reopened, truncated or rewritten - and every record carries the
SHA-256 of its own canonical JSON, so a later edit is detectable.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
Clock = Callable[[], datetime]

# A transport sends one HTTP request and returns (status, body text). It
# raises OSError (incl. urllib.error.URLError) when no HTTP response exists.
Transport = Callable[[str, str, float], tuple[int, str]]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def stamp(moment: datetime) -> dict[str, str]:
    """A timezone-aware moment as both UTC and IST ISO-8601 strings."""
    if moment.tzinfo is None:
        raise ValueError("evidence timestamps must be timezone-aware")
    return {"utc": moment.astimezone(timezone.utc).isoformat(), "ist": moment.astimezone(IST).isoformat()}


def encode_number(value: Any) -> Any:
    """JSON has no NaN/Infinity: non-finite floats are kept as the strings
    "NaN", "Infinity", "-Infinity" - recorded, never replaced by a number."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    number = float(value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"
    return number


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if hasattr(value, "item"):  # numpy scalars
        return encode_number(value.item())
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _sanitize_floats(value: Any) -> Any:
    if isinstance(value, float):
        return encode_number(value)
    if isinstance(value, dict):
        return {key: _sanitize_floats(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_sanitize_floats(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(_sanitize_floats(value), sort_keys=True, separators=(",", ":"),
                      default=_json_default, allow_nan=False, ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


_SECRET_PATTERNS = [
    # Bearer first: "Authorization=Bearer <token>" must lose the token, not just the word.
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
    (re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|authorization|access[_-]?key)"
                r"(\s*[=:]\s*)(?!Bearer <redacted>)(\"[^\"]*\"|'[^']*'|[^;&,\s]+)"), r"\1\2<redacted>"),
    (re.compile(r"://[^/@\s]+@"), "://<redacted>@"),
]


def redact(text: str, limit: int = 500) -> str:
    """Removes anything credential-shaped from free text (error messages,
    URLs) before it can reach an evidence file."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:limit]


def describe_error(error: BaseException) -> dict[str, str]:
    return {"type": type(error).__name__, "message": redact(str(error))}


class EvidenceFile:
    """One append-only JSONL evidence file per tool run. Created with mode
    "x": a file that already exists is an error, never overwritten."""

    def __init__(self, directory: Path, kind: str, run_id: str, started: datetime) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{kind}_{started.astimezone(IST):%Y%m%dT%H%M%S%z}_{run_id}.jsonl"
        self.path = directory / name
        self._handle = self.path.open("x", encoding="utf-8")

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        body = dict(record)
        body.pop("record_sha256", None)
        sealed = {**body, "record_sha256": sha256_text(canonical_json(body))}
        self._handle.write(canonical_json(sealed) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        return sealed

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> EvidenceFile:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def verify_record(record: dict[str, Any]) -> bool:
    """True if a record's own record_sha256 still matches its content."""
    body = {key: value for key, value in record.items() if key != "record_sha256"}
    return record.get("record_sha256") == sha256_text(canonical_json(body))


def validate_base_url(base_url: str, *, allow_non_loopback: bool = False) -> str:
    """The dashboard base URL: http(s), no credentials, no path/query. By
    default only a loopback host (the dashboard binds 127.0.0.1)."""
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("base URL must be http(s)://host[:port]")
    if parts.username or parts.password:
        raise ValueError("base URL must not contain credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("base URL must not contain a path, query or fragment")
    if not allow_non_loopback and not _is_loopback(parts.hostname):
        raise ValueError(f"refusing non-loopback host {parts.hostname!r} (pass allow_non_loopback)")
    return f"{parts.scheme}://{parts.netloc}"


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def urllib_transport(method: str, url: str, timeout: float) -> tuple[int, str]:
    """The default transport. An HTTP error status (4xx/5xx) is a response,
    returned with its body - never raised or retried."""
    req = urlrequest.Request(url, method=method, headers={"Accept": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:  # noqa: S310 - validated http(s) URL
            return response.status, response.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")


@dataclass(frozen=True)
class HttpExchange:
    method: str
    path: str
    sent_at: datetime
    received_at: datetime
    elapsed_seconds: float
    http_status: int | None
    body_json: Any
    body_text: str | None
    error: dict[str, str] | None

    def to_record(self) -> dict[str, Any]:
        return {
            "method": self.method, "path": self.path,
            "sent_at": stamp(self.sent_at), "received_at": stamp(self.received_at),
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "http_status": self.http_status, "body_json": self.body_json,
            "body_text": self.body_text, "error": self.error,
        }


def exchange(transport: Transport, method: str, base_url: str, path: str, timeout: float,
             clock: Clock = utc_now) -> HttpExchange:
    """One request, fully recorded: a network failure, a non-JSON body and
    an HTTP error status are all kept as they are - nothing is assumed."""
    sent_at = clock()
    started = time.monotonic()
    status: int | None = None
    body_json: Any = None
    body_text: str | None = None
    error: dict[str, str] | None = None
    try:
        status, text = transport(method, base_url + path, timeout)
        try:
            body_json = json.loads(text)
        except ValueError:
            body_text = redact(text, limit=2000)
    except (OSError, ValueError) as failure:
        error = describe_error(failure)
    elapsed = time.monotonic() - started
    return HttpExchange(method, path, sent_at, clock(), elapsed, status, body_json, body_text, error)
