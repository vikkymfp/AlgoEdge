"""Server-side login sessions and request rate limiting (in process memory).

A session is identified by a 256-bit random token sent only in an HttpOnly
cookie. The store keys sessions by the token's SHA-256, so the raw token
exists only in the browser's cookie; `reference` - a short prefix of that
digest - is what logs and user_login_activity.session_id record, and it can
never be turned back into a usable cookie.

Sessions expire after an idle timeout and an absolute lifetime, are never
extended past the absolute limit, and are dropped on logout. A restart of
the (single-process) dashboard therefore signs everyone out - by design, no
authentication state is written to disk.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_reference(token: str) -> str:
    """Non-secret, stable label for a session (safe for logs and the activity table)."""
    return _digest(token)[:32]


@dataclass
class Session:
    user_id: int
    role: str
    email: str | None
    mobile_no: str | None
    csrf_token: str
    reference: str
    created_at: datetime
    last_seen_at: datetime
    validated_at: datetime


class SessionStore:
    def __init__(self, *, idle: timedelta, absolute: timedelta, clock: Callable[[], datetime]) -> None:
        self.idle = idle
        self.absolute = absolute
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, *, user_id: int, role: str, email: str | None, mobile_no: str | None,
               token: str | None = None) -> tuple[str, Session]:
        """A brand-new session with a fresh random token (never one the client chose)."""
        token = token or secrets.token_urlsafe(32)
        now = self._clock()
        session = Session(user_id=user_id, role=role, email=email, mobile_no=mobile_no,
                          csrf_token=secrets.token_urlsafe(32), reference=session_reference(token),
                          created_at=now, last_seen_at=now, validated_at=now)
        with self._lock:
            self._prune(now)
            self._sessions[_digest(token)] = session
        return token, session

    @staticmethod
    def new_token() -> str:
        return secrets.token_urlsafe(32)

    def get(self, token: str | None, *, touch: bool = True) -> Session | None:
        """The live session for `token`, or None (unknown, idle-expired or past
        its absolute lifetime - an expired session is deleted, never revived)."""
        if not token:
            return None
        key = _digest(token)
        now = self._clock()
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            if now - session.last_seen_at >= self.idle or now - session.created_at >= self.absolute:
                del self._sessions[key]
                return None
            if touch:
                session.last_seen_at = now
            return session

    def revoke(self, token: str | None) -> Session | None:
        if not token:
            return None
        with self._lock:
            return self._sessions.pop(_digest(token), None)

    def revoke_user(self, user_id: int) -> int:
        """Ends every session of one user (deactivation, role change, password reset)."""
        with self._lock:
            keys = [key for key, session in self._sessions.items() if session.user_id == user_id]
            for key in keys:
                del self._sessions[key]
            return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def _prune(self, now: datetime) -> None:
        expired = [key for key, session in self._sessions.items()
                   if now - session.last_seen_at >= self.idle or now - session.created_at >= self.absolute]
        for key in expired:
            del self._sessions[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


class RateLimiter:
    """Sliding-window counter: at most `limit` hits per key per `window`."""

    def __init__(self, limit: int, window: timedelta, clock: Callable[[], datetime]) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._hits: dict[str, deque[datetime]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """Records one hit; False when the key is already over its limit (the hit is not counted)."""
        now = self._clock()
        with self._lock:
            if len(self._hits) > 50_000:
                self._hits = {k: q for k, q in self._hits.items() if q and now - q[-1] < self.window}
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()
