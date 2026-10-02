"""Protects the unauthenticated localhost dashboard API from other web pages.

The server binds to 127.0.0.1 with no login, so without these checks any
site open in the user's browser could drive it: a cross-site POST needs no
CORS preflight when it carries no body, and DNS rebinding makes a hostile
page same-origin with the dashboard. Three rules close both routes:

* the Host header must be a loopback name (or one the operator allow-listed);
* a state-changing request's Origin, when present, must match its Host;
* a state-changing request must carry ``X-AlgoEdge-Request``. A custom header
  forces a CORS preflight for cross-origin callers, which is never granted.
"""

from __future__ import annotations

CSRF_HEADER = "x-algoedge-request"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})


def _hostname(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[: host.find("]") + 1] if "]" in host else host
    return host.rsplit(":", 1)[0] if ":" in host else host


def parse_allowed_hosts(value: str) -> frozenset[str]:
    return frozenset(item.strip().lower() for item in value.split(",") if item.strip())


def rejection_reason(
    method: str,
    host: str | None,
    origin: str | None,
    csrf_header: str | None,
    extra_hosts: frozenset[str] = frozenset(),
) -> str | None:
    """Returns why the request must be refused, or None to let it through."""
    if not host or _hostname(host) not in LOOPBACK_HOSTS | extra_hosts:
        return "Host not allowed"
    if method.upper() in SAFE_METHODS:
        return None
    if origin is not None:
        origin_host = origin.split("://", 1)[-1].lower()
        if origin == "null" or origin_host != host.strip().lower():
            return "Cross-origin request refused"
    if not csrf_header:
        return f"Missing {CSRF_HEADER} header"
    return None
