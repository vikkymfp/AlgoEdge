from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import requests
from growwapi import GrowwAPI
from growwapi.groww.exceptions import (
    GrowwAPIAuthenticationException,
    GrowwAPIAuthorisationException,
    GrowwAPIException,
    GrowwAPINotFoundException,
)

from algoedge import credential_manager, db
from algoedge.config import Settings
from algoedge.credential_manager import CredentialEncryptionUnavailable
from algoedge.risk_manager import IST

logger = logging.getLogger("algoedge.token_service")

# Credential model: the Groww API key + secret are the persistent
# credentials. The access token is SESSION state minted from them by
# GrowwAPI.get_access_token(api_key, secret=...) - growwapi POSTs
# {"key_type": "approval", "checksum": sha256(secret + timestamp),
# "timestamp": ...} to /v1/token/api/access with the key as Bearer and gets
# back only a token string (no expiry). GrowwAPI(token) then sends that
# token on every call and has no refresh logic of its own - an expired
# session just starts failing with GrowwAPIAuthenticationException (401).
# Re-authenticating (minting a new session from the key/secret) is
# therefore this service's job - see _reauthenticate().
#
# Groww sessions reset daily around 6:00 AM IST - documented platform
# behavior, not a value any response publishes. This is therefore always an
# ESTIMATE, used for the "Time Remaining"/"Expiring Soon" UI warning and to
# renew a key/secret session proactively once it has passed. It never
# substitutes for real detection: connection_status is set only from an
# actual call succeeding or failing - the estimate never marks a connection
# CONNECTED or downgrades it to TOKEN_EXPIRED on its own, and a failed
# proactive renewal keeps the still-working session.
_TOKEN_DAILY_RESET_HOUR = 6
_EXPIRING_SOON_WINDOW_MINUTES = 30
# Minimum gap between automatic re-authentication attempts, so a key that
# can't mint a session (e.g. Groww's daily approval not yet given) isn't
# hammered by every poll. An explicit reauthenticate() ignores it.
_REAUTH_COOLDOWN_SECONDS = 60

# How the current session is obtained - see TokenService.auth_mode().
AUTH_MODE_API_KEY_SECRET = "API_KEY_SECRET"  # minted from key/secret; renewed automatically
AUTH_MODE_MANUAL_TOKEN = "MANUAL_TOKEN"  # a session token supplied directly (e.g. TOTP-type keys); no auto-renewal
AUTH_MODE_NONE = "NONE"


def _as_ist(value: datetime | None) -> datetime | None:
    """Database DATETIME columns drop the timezone but keep the IST wall
    time these values were written with - restore it, so they compare
    safely with datetime.now(IST)."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=IST)


def _estimate_token_expiry(created_at: datetime) -> datetime:
    local = created_at.astimezone(IST)
    reset_at = local.replace(hour=_TOKEN_DAILY_RESET_HOUR, minute=0, second=0, microsecond=0)
    if local >= reset_at:
        reset_at += timedelta(days=1)
    return reset_at

BROKER_NAME = "groww"

# Exceptions that can surface from a real Groww call (auth failure, bad
# request, rate limit, timeout, or a genuine network/connectivity problem)
# or from growwapi's static get_access_token helper, which uses `requests`
# directly and isn't wrapped in GrowwAPIException on connection failures.
_GROWW_CALL_ERRORS = (GrowwAPIException, requests.RequestException, OSError, TypeError, ValueError, KeyError)

# How a failed Groww call is classified - see classify_failure().
FAILURE_AUTH = "AUTH"  # the token/session itself is bad -> broker-level
FAILURE_CAPABILITY = "CAPABILITY"  # this endpoint/feature isn't permitted -> endpoint-level only
FAILURE_TRANSIENT = "TRANSIENT"  # timeout/network/5xx/unexpected -> broker ERROR, client kept

# Groww reports some failures as a 200 with {"status": "FAILURE"} and its own
# error code, so the HTTP status is lost and only the message is left to go
# on. Auth markers are checked FIRST: anything that could mean the token or
# session is bad is always treated as an auth failure (the safe side).
_AUTH_MESSAGE_MARKERS = (
    "unauthori", "authenticat", "token expired", "expired token", "invalid token",
    "token is invalid", "token invalid", "session expired", "session invalid", "invalid session", "login",
)
_CAPABILITY_MESSAGE_MARKERS = (
    "forbidden", "not permitted", "permission", "not subscribed", "not enabled", "not allowed",
    "not supported", "unsupported",
)

# Groww client methods grouped into the capabilities Diagnostics reports on.
# A method not listed here is tracked under its own name.
_ENDPOINT_CAPABILITIES = {
    "get_user_profile": "profile",
    "get_available_margin_details": "margin",
    "get_holdings_for_user": "holdings",
    "get_positions_for_user": "positions",
    "get_position_for_trading_symbol": "positions",
    "get_order_list": "orders",
    "get_order_status": "orders",
    "get_order_status_by_reference": "orders",
    "get_order_detail": "orders",
    "get_trade_list_for_order": "orders",
    "place_order": "order_placement",
    "modify_order": "order_placement",
    "cancel_order": "order_placement",
    "get_quote": "market_data",
    "get_ltp": "market_data",
    "get_ohlc": "market_data",
    "get_historical_candle_data": "market_data",
    "get_historical_candles": "market_data",
    "get_greeks": "market_data",
    "get_option_chain": "market_data",
    "get_expiries": "market_data",
    "get_contracts": "market_data",
    "get_all_instruments": "instrument_master",
    "get_instrument_by_groww_symbol": "instrument_master",
    "get_instrument_by_exchange_and_trading_symbol": "instrument_master",
    "get_instrument_by_exchange_token": "instrument_master",
}


def capability_for(endpoint: str | None) -> str | None:
    if endpoint is None:
        return None
    return _ENDPOINT_CAPABILITIES.get(endpoint, endpoint)


def classify_failure(error: Exception) -> str:
    """AUTH only for a genuinely bad token/session (401, or a Groww
    FAILURE message that says so); CAPABILITY for a 403/404 or a message
    saying this specific endpoint/feature isn't permitted; everything
    else (timeouts, network, rate limits, 5xx, unexpected shapes) is
    TRANSIENT. Auth is checked before capability, so an ambiguous message
    never downgrades a real auth failure to endpoint-level."""
    if isinstance(error, GrowwAPIAuthenticationException):
        return FAILURE_AUTH
    if not isinstance(error, GrowwAPIException):
        return FAILURE_TRANSIENT
    code = str(getattr(error, "code", "") or "")
    message = (getattr(error, "msg", None) or str(error)).lower()
    if code == "401" or any(marker in message for marker in _AUTH_MESSAGE_MARKERS):
        return FAILURE_AUTH
    if isinstance(error, (GrowwAPIAuthorisationException, GrowwAPINotFoundException)) or code in {"403", "404"}:
        return FAILURE_CAPABILITY
    if any(marker in message for marker in _CAPABILITY_MESSAGE_MARKERS):
        return FAILURE_CAPABILITY
    return FAILURE_TRANSIENT


class BrokerNotConnectedError(RuntimeError):
    """Raised when trading/broker-data code asks for a client but no
    working Groww connection is currently established. Callers must catch
    this and degrade gracefully (block trading, show a clear message) -
    never let it propagate as a raw 500."""


class BrokerValidationError(RuntimeError):
    """Raised when a credential/token update fails validation. The message
    is always safe to show the user and log - see TokenService._safe_message."""


@dataclass(frozen=True)
class BrokerStatus:
    broker: str
    api_key_masked: str | None
    api_secret_masked: str | None
    # Describes the current authenticated SESSION (the generated access
    # token), never the API key/secret themselves, which don't expire daily.
    # ACTIVE | EXPIRING_SOON | RENEWAL_DUE | EXPIRED | INVALID | UNAVAILABLE.
    # EXPIRED/INVALID only ever come from a real authentication failure;
    # RENEWAL_DUE means the estimated daily reset has passed but the
    # session is still actually working (renewal pending or failed).
    token_status: str
    connection_status: str  # CONNECTED | TOKEN_EXPIRED | TOKEN_INVALID | DISCONNECTED | MISSING | ERROR
    token_created_at: datetime | None
    token_expiry_at: datetime | None
    token_expiry_is_estimated: bool
    last_validated_at: datetime | None
    last_successful_request_at: datetime | None
    last_error: str | None
    credentials_persisted: bool
    auth_mode: str = AUTH_MODE_NONE
    auto_reauth_available: bool = False
    # Endpoint-level availability, e.g. {"market_data": {"status":
    # "UNAVAILABLE", "error": "...", "checkedAt": ...}}. Only capabilities
    # actually exercised since the last (re)connection appear here; none of
    # them ever changes connection_status on its own.
    capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectionTestResult:
    connected: bool
    message: str


class _TrackedClient:
    """Thin transparent proxy around a real GrowwAPI client that calls back
    into TokenService after any method call succeeds OR fails, so both
    "Last Successful API Request" AND connection_status reflect actual
    usage across the whole app (Market Pulse's account calls, Manual
    Trading, grid/positions/orders panels, reconciliation) rather than only
    the handful of calls TokenService makes directly itself. This is what
    makes "Groww Connected" a live signal instead of a value that's only
    ever updated by an explicit Test Connection click - a token that
    expires mid-session gets caught by the very next real call anyone in
    the app makes through this client, not just a dedicated health check.
    Never swallows an exception - it is always re-raised after being
    reported, so callers see the real failure."""

    def __init__(self, client: GrowwAPI, on_success: Any, on_failure: Any) -> None:
        self._client = client
        self._on_success = on_success
        self._on_failure = on_failure

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._client, name)
        if not callable(attribute):
            return attribute

        def _tracked(*args: Any, **kwargs: Any) -> Any:
            try:
                result = attribute(*args, **kwargs)
            except _GROWW_CALL_ERRORS as error:
                self._on_failure(error, endpoint=name)
                raise
            self._on_success(endpoint=name)
            return result

        return _tracked


class TokenService:
    """Owns the one Groww connection AlgoEdge uses everywhere - Market
    Pulse's account calls, Manual Trading, Auto Trading, fno_signals --live.
    Nothing else in the app should call GrowwAPI.get_access_token or
    construct a GrowwAPI client directly; everything goes through
    effective_client()/is_connected() so there is exactly one place that
    knows about credentials, encryption, and connection state.

    Groww's officially supported non-interactive auth is the "approval" key
    type: an API key + secret, where a fresh access token can be minted at
    any time via a SHA-256 checksum of secret+timestamp (see growwapi's own
    GrowwAPI.get_access_token(api_key=..., secret=...) - this is the exact
    call this service uses, not an invented flow). That makes automatic
    token refresh possible for approval-type keys without any manual step.
    A TOTP-type key (or simply pasting a token obtained through Groww's own
    login flow) has no such automated path in the SDK, so the manual
    "Update Access Token" workflow exists as the fallback for that case -
    per the user's own instruction, this service never invents or guesses a
    token, it only validates and stores one the user (or the approval flow)
    actually produced.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._api_key: str | None = settings.groww_api_key or None
        self._api_secret: str | None = settings.groww_api_secret or None
        self._access_token: str | None = settings.groww_access_token or None
        self._token_created_at: datetime | None = None
        self._token_expiry_at: datetime | None = None
        self._last_validated_at: datetime | None = None
        self._last_successful_request_at: datetime | None = None
        self._connection_status = "MISSING"
        self._last_error: str | None = None
        self._client: GrowwAPI | None = None
        self._capabilities: dict[str, dict[str, Any]] = {}
        self._last_reauth_attempt_at: datetime | None = None
        self._reauth_failing = False
        self._last_reauth_error: str | None = None
        # _TrackedClient callbacks can arrive concurrently (e.g.
        # account_snapshot's thread pool) - transitions happen under this.
        self._state_lock = threading.RLock()
        # Whether the most recent successful update_access_token()/
        # update_credentials() call actually stored the new secrets
        # (encrypted) in the database. None until an update succeeds. Says
        # nothing about whether the broker is connected right now - that is
        # always current_connection_status().
        self.last_update_persisted: bool | None = None
        self._load_from_db()
        self.auto_refresh_if_needed()

    # -- loading -----------------------------------------------------

    def _load_from_db(self) -> None:
        row = db.load_broker_credential(BROKER_NAME)
        if row is None:
            return
        key = self._settings.credential_encryption_key
        try:
            if row["encryptedApiKey"] and key:
                self._api_key = credential_manager.decrypt(row["encryptedApiKey"], key)
            if row["encryptedApiSecret"] and key:
                self._api_secret = credential_manager.decrypt(row["encryptedApiSecret"], key)
            if row["encryptedAccessToken"] and key:
                self._access_token = credential_manager.decrypt(row["encryptedAccessToken"], key)
        except CredentialEncryptionUnavailable as error:
            logger.warning("Stored broker credentials could not be decrypted, ignoring: %s", error)
        self._token_created_at = _as_ist(row["tokenCreatedAt"])
        self._token_expiry_at = _as_ist(row["tokenExpiryAt"])
        self._last_validated_at = _as_ist(row["lastValidatedAt"])
        self._last_successful_request_at = _as_ist(row["lastSuccessfulRequestAt"])
        self._connection_status = row["connectionStatus"] or "MISSING"
        self._last_error = row["lastError"]

    # -- public read API ----------------------------------------------

    def current_connection_status(self) -> str:
        """The one authoritative, current connection state - what the
        header pill, API Management, Diagnostics and System Health all
        show. CONNECTED only while a validated client is actually held;
        a stored CONNECTED with no live client is reported as DISCONNECTED
        rather than trusted."""
        if self._connection_status == "CONNECTED" and self._client is None:
            return "DISCONNECTED"
        return self._connection_status

    def is_connected(self) -> bool:
        return self.current_connection_status() == "CONNECTED"

    def auth_mode(self) -> str:
        if self._api_key and self._api_secret:
            return AUTH_MODE_API_KEY_SECRET
        if self._access_token:
            return AUTH_MODE_MANUAL_TOKEN
        return AUTH_MODE_NONE

    def effective_client(self) -> GrowwAPI:
        """The one way callers get a Groww client. With an API key/secret
        configured, a session that is known to be dead (expired/invalid
        token) or past its estimated daily reset is renewed here first -
        see _reauthenticate(). The call that failed is never retried on the
        caller's behalf (an order is never silently re-sent); only calls
        made after renewal use the new session."""
        with self._state_lock:
            if self.auth_mode() == AUTH_MODE_API_KEY_SECRET:
                if self._client is None:
                    self._reauthenticate("SESSION_EXPIRED")
                elif self._session_past_estimated_expiry():
                    self._reauthenticate("SCHEDULED_RENEWAL")
        if self._client is None:
            raise BrokerNotConnectedError(
                self._last_error or "Groww is not connected. Configure it in API Management."
            )
        return _TrackedClient(self._client, self.mark_successful_request, self.mark_failed_request)  # type: ignore[return-value]

    def _session_past_estimated_expiry(self) -> bool:
        return self._token_expiry_at is not None and datetime.now(IST) >= self._token_expiry_at

    def _mint_session(self, api_key: str, api_secret: str) -> str:
        """Generates a new session from the API key/secret (Groww's
        approval-checksum flow) and validates it. Raises on failure without
        touching the current session."""
        token = GrowwAPI.get_access_token(api_key=api_key, secret=api_secret)
        self._activate(token)
        return token

    def _reauthenticate(self, reason: str, *, force: bool = False) -> bool:
        """Mints a fresh session from the stored API key/secret. Returns
        whether it succeeded. On failure the current session, if any, is
        kept untouched (a proactive renewal must never drop a still-working
        session), connection_status is left as it was, and the error is
        recorded - once per failing streak, not on every retry. Automatic
        attempts are rate-limited by _REAUTH_COOLDOWN_SECONDS."""
        if not (self._api_key and self._api_secret):
            return False
        now = datetime.now(IST)
        if (
            not force and self._last_reauth_attempt_at is not None
            and (now - self._last_reauth_attempt_at).total_seconds() < _REAUTH_COOLDOWN_SECONDS
        ):
            return False
        self._last_reauth_attempt_at = now
        try:
            token = self._mint_session(self._api_key, self._api_secret)
        except _GROWW_CALL_ERRORS as error:
            message = f"Re-authentication failed: {self._safe_message(error)}"[:255]
            self._last_reauth_error = message
            if self._client is None:
                self._last_error = message
            if not self._reauth_failing or force:
                self._record_event("REAUTH_FAILED", status="FAILED", error_message=f"{reason}: {message}"[:255])
            self._reauth_failing = True
            self._persist()
            return False
        self._reauth_failing = False
        self._set_fresh_token_lifecycle()
        self._record_event(
            "SESSION_REAUTHENTICATED", status="SUCCESS",
            token_reference=credential_manager.reference_hint(token), error_message=reason,
        )
        self._persist()
        return True

    def reauthenticate(self) -> BrokerStatus:
        """The explicit "Re-authenticate now" action: mints a new session
        from the stored key/secret immediately, ignoring the cooldown (e.g.
        right after approving API access in the Groww app)."""
        with self._state_lock:
            if self.auth_mode() != AUTH_MODE_API_KEY_SECRET:
                raise ValueError("Re-authentication needs a Groww API key and secret. Add them in API Management.")
            if not self._reauthenticate("MANUAL", force=True):
                raise BrokerValidationError(self._last_reauth_error or "Re-authentication failed.")
        return self.status()

    def mark_successful_request(self, endpoint: str | None = None) -> None:
        """Called by _TrackedClient after any real Groww API call succeeds.
        Marks that endpoint's capability AVAILABLE, and - because a real
        authenticated call just worked - restores CONNECTED from a
        transient ERROR. Never revives a TOKEN_EXPIRED/TOKEN_INVALID
        connection: an auth failure discards the client, so no tracked call
        can succeed until a new token is validated."""
        with self._state_lock:
            now = datetime.now(IST)
            self._last_successful_request_at = now
            capability = capability_for(endpoint)
            if capability is not None:
                self._capabilities[capability] = {"status": "AVAILABLE", "error": None, "checkedAt": now}
            if self._connection_status == "ERROR" and self._client is not None:
                self._connection_status = "CONNECTED"
                self._last_error = None
                self._record_event("CONNECTION_RESTORED", status="SUCCESS")
                self._persist()

    def mark_failed_request(self, error: Exception, endpoint: str | None = None) -> None:
        """Called by _TrackedClient after ANY real Groww call made anywhere
        in the app fails - see classify_failure() for the three outcomes.

        AUTH: the token itself is now confirmed bad (expired if it was
        previously validated at least once, invalid if it never was) and
        the client is discarded so no further call can be attempted against
        a token already known to be dead.

        CAPABILITY (e.g. a 403 on market data the account isn't subscribed
        to): only that capability is marked UNAVAILABLE. The session is
        still authenticated, so connection_status is left as it is.

        TRANSIENT (timeout, network error, unexpected response): the broker
        is marked ERROR but the client is kept - a blip shouldn't force a
        real reconnect, and the next successful call restores CONNECTED.

        Only writes to the DB/audit log on an actual transition, not on
        every repeated failure while already broken."""
        with self._state_lock:
            message = self._safe_message(error)
            kind = classify_failure(error)
            if kind == FAILURE_CAPABILITY:
                capability = capability_for(endpoint) or "unknown"
                previous = self._capabilities.get(capability, {}).get("status")
                self._capabilities[capability] = {
                    "status": "UNAVAILABLE", "error": message, "checkedAt": datetime.now(IST),
                }
                if previous != "UNAVAILABLE":
                    self._record_event(
                        "CAPABILITY_UNAVAILABLE", status="FAILED", error_message=f"{capability}: {message}"[:255],
                    )
                return

            previous_status = self._connection_status
            self._last_error = message
            if kind == FAILURE_AUTH:
                self._connection_status = "TOKEN_EXPIRED" if self._last_validated_at else "TOKEN_INVALID"
                self._client = None
                self._capabilities = {}
            else:
                self._connection_status = "ERROR"
            if self._connection_status != previous_status:
                self._record_event("CONNECTION_LOST", status="FAILED", error_message=self._last_error)
                self._persist()

    def _compute_token_status(self) -> str:
        """ACTIVE | EXPIRING_SOON | RENEWAL_DUE | EXPIRED | INVALID |
        UNAVAILABLE - always derived from connection_status (real evidence)
        first. The estimated daily-reset clock only ever adds an
        EXPIRING_SOON/RENEWAL_DUE note on top of an otherwise-CONNECTED
        state - never EXPIRED, which needs a real authentication failure; it never overrides
        connection_status itself (see the module docstring on
        _estimate_token_expiry) - the next real call self-corrects it
        either way, typically within seconds given how often this app
        polls the broker."""
        # A confirmed-bad result (real evidence) always wins, even when the
        # rejected credentials themselves were never stored (a failed
        # update attempt) - "no credential on file" and "the credential you
        # just tried was rejected" are different, and the latter is the
        # more informative/accurate thing to show.
        connection_status = self.current_connection_status()
        if connection_status == "TOKEN_INVALID":
            return "INVALID"
        if connection_status == "TOKEN_EXPIRED":
            return "EXPIRED"
        has_any_credential = bool(self._access_token or (self._api_key and self._api_secret))
        if not has_any_credential:
            return "UNAVAILABLE"
        if connection_status != "CONNECTED":
            return "UNAVAILABLE"
        if self._token_expiry_at is not None:
            remaining_seconds = (self._token_expiry_at - datetime.now(IST)).total_seconds()
            if remaining_seconds <= 0:
                # Only an estimate has passed - the session is still
                # working (connection_status is CONNECTED), so it is due
                # for renewal, not expired.
                return "RENEWAL_DUE"
            if remaining_seconds <= _EXPIRING_SOON_WINDOW_MINUTES * 60:
                return "EXPIRING_SOON"
        return "ACTIVE"

    def status(self) -> BrokerStatus:
        return BrokerStatus(
            broker=BROKER_NAME,
            api_key_masked=credential_manager.mask_key(self._api_key),
            api_secret_masked=credential_manager.mask_secret(self._api_secret),
            token_status=self._compute_token_status(),
            connection_status=self.current_connection_status(),
            token_created_at=self._token_created_at,
            token_expiry_at=self._token_expiry_at,
            token_expiry_is_estimated=True,
            last_validated_at=self._last_validated_at,
            last_successful_request_at=self._last_successful_request_at,
            last_error=self._last_error,
            credentials_persisted=credential_manager.is_configured(self._settings.credential_encryption_key)
            and db.is_available(),
            auth_mode=self.auth_mode(),
            auto_reauth_available=self.auth_mode() == AUTH_MODE_API_KEY_SECRET,
            capabilities={name: dict(value) for name, value in self._capabilities.items()},
        )

    def capability_status(self, capability: str) -> dict[str, Any] | None:
        """This capability's last observed availability, or None if it
        hasn't been exercised since the last (re)connection."""
        value = self._capabilities.get(capability)
        return dict(value) if value is not None else None

    # -- activation ------------------------------------------------------

    def _activate(self, access_token: str) -> None:
        """Builds a client from this token and confirms it actually works
        via a safe, read-only probe (get_user_profile - never places an
        order). Raises on failure; never marks a connection healthy without
        having proven it first."""
        client = GrowwAPI(access_token)
        profile = client.get_user_profile()
        if not isinstance(profile, dict):
            raise TypeError("Unexpected response validating the Groww connection.")
        self._client = client
        self._access_token = access_token
        now = datetime.now(IST)
        # A newly validated token may carry different permissions - start
        # its capability map fresh from what this probe just proved.
        self._capabilities = {"profile": {"status": "AVAILABLE", "error": None, "checkedAt": now}}
        self._connection_status = "CONNECTED"
        self._last_validated_at = now
        self._last_successful_request_at = now
        self._last_error = None

    def _safe_message(self, error: Exception) -> str:
        """Groww's own exception messages are fixed, generic strings (e.g.
        "Authentication failed...") that never embed the submitted
        key/secret/token, so this is safe to log and show as-is - just
        trimmed to fit the audit table's column."""
        message = getattr(error, "msg", None) or str(error)
        return message[:255]

    def _persist(self) -> bool:
        """Returns whether the credentials themselves (not just status
        metadata) were written, encrypted, to the database."""
        key = self._settings.credential_encryption_key
        fields: dict[str, Any] = {
            "token_created_at": self._token_created_at,
            "token_expiry_at": self._token_expiry_at,
            "last_validated_at": self._last_validated_at,
            "last_successful_request_at": self._last_successful_request_at,
            "connection_status": self._connection_status,
            "last_error": self._last_error,
        }
        if credential_manager.is_configured(key):
            if self._api_key:
                fields["api_key_hint"] = credential_manager.mask_key(self._api_key)
                fields["encrypted_api_key"] = credential_manager.encrypt(self._api_key, key)
            if self._api_secret:
                fields["encrypted_api_secret"] = credential_manager.encrypt(self._api_secret, key)
            if self._access_token:
                fields["encrypted_access_token"] = credential_manager.encrypt(self._access_token, key)
        saved = db.save_broker_credential(BROKER_NAME, **fields)
        return saved and credential_manager.is_configured(key)

    def _record_event(
        self, event: str, *, status: str, token_reference: str | None = None, error_message: str | None = None,
    ) -> None:
        db.record_token_audit_event(
            broker=BROKER_NAME, event=event, status=status,
            token_reference=token_reference, error_message=error_message,
        )

    def _set_fresh_token_lifecycle(self) -> None:
        """A genuinely new token value was just minted or supplied - resets
        the creation clock and recomputes the estimated expiry from it."""
        self._token_created_at = datetime.now(IST)
        self._token_expiry_at = _estimate_token_expiry(self._token_created_at)

    def _ensure_token_lifecycle_initialized(self) -> None:
        """The same previously-known token was just re-validated (e.g. at
        app startup, re-using a stored/env token) - never slides the
        creation clock forward on every restart of an already-known token.
        Also backfills a missing expiry estimate from an existing creation
        time (covers credentials persisted before this feature existed,
        when token_expiry_at was always stored as None)."""
        if self._token_created_at is None:
            self._set_fresh_token_lifecycle()
        elif self._token_expiry_at is None:
            self._token_expiry_at = _estimate_token_expiry(self._token_created_at)

    def _handle_failed_activation(self, error: Exception) -> str:
        """A failed *update* attempt (new token/credentials that turned out
        to be bad) must never clobber an already-working connection -
        self._client is untouched by a failed _activate() call, so it's
        still perfectly usable. Only downgrade connection_status when there
        was nothing working before this attempt; otherwise just record
        last_error as context about the failed attempt while leaving the
        existing good connection marked CONNECTED."""
        message = self._safe_message(error)
        if self._client is None:
            if classify_failure(error) == FAILURE_AUTH:
                self._connection_status = "TOKEN_EXPIRED" if self._last_validated_at else "TOKEN_INVALID"
            else:
                self._connection_status = "ERROR"
        self._last_error = message
        return message

    # -- update workflows --------------------------------------------

    def update_access_token(self, raw_token: str) -> BrokerStatus:
        """The advanced "use a session token instead" workflow, for keys
        that can't mint sessions from a secret (e.g. TOTP keys): validates
        the pasted session token against a real, safe/read-only Groww
        endpoint BEFORE storing anything. Never invents a token - this only
        ever stores exactly what the caller supplied. Such a session is not
        renewed automatically unless an API key/secret is also configured."""
        raw_token = (raw_token or "").strip()
        if not raw_token:
            raise ValueError("Access token cannot be empty.")
        try:
            self._activate(raw_token)
        except _GROWW_CALL_ERRORS as error:
            message = self._handle_failed_activation(error)
            self._record_event(
                "TOKEN_UPDATED", status="FAILED",
                token_reference=credential_manager.reference_hint(raw_token), error_message=message,
            )
            self._persist()
            raise BrokerValidationError(message) from error
        self._set_fresh_token_lifecycle()
        self.last_update_persisted = self._persist()
        self._record_event(
            "TOKEN_UPDATED", status="SUCCESS", token_reference=credential_manager.reference_hint(raw_token),
        )
        return self.status()

    def update_credentials(self, api_key: str, api_secret: str) -> BrokerStatus:
        """Configures the API key/secret and immediately mints + validates
        a fresh access token via Groww's own approval-checksum flow
        (GrowwAPI.get_access_token) - the same call this app already used
        successfully for auto-auth before this module existed."""
        api_key = (api_key or "").strip()
        api_secret = (api_secret or "").strip()
        if not api_key or not api_secret:
            raise ValueError("API key and API secret are both required.")
        try:
            self._mint_session(api_key, api_secret)
        except _GROWW_CALL_ERRORS as error:
            message = self._handle_failed_activation(error)
            self._record_event(
                "CREDENTIALS_UPDATED", status="FAILED",
                token_reference=credential_manager.reference_hint(api_key), error_message=message,
            )
            self._persist()
            raise BrokerValidationError(message) from error
        self._api_key = api_key
        self._api_secret = api_secret
        self._reauth_failing = False
        self._set_fresh_token_lifecycle()
        self.last_update_persisted = self._persist()
        self._record_event(
            "CREDENTIALS_UPDATED", status="SUCCESS", token_reference=credential_manager.reference_hint(api_key),
        )
        return self.status()

    def test_connection(self) -> ConnectionTestResult:
        """Re-validates whatever connection is currently active via the
        same safe/read-only probe used everywhere else in this service.
        Never places an order."""
        if self._client is None:
            has_any_credential = bool(self._access_token or (self._api_key and self._api_secret))
            self._connection_status = "MISSING" if not has_any_credential else self._connection_status
            message = "Not connected. Configure API credentials or an access token in API Management."
            self._record_event("VALIDATION_FAILED", status="FAILED", error_message=message)
            self._persist()
            return ConnectionTestResult(False, message)
        try:
            profile = self._client.get_user_profile()
            if not isinstance(profile, dict):
                raise TypeError("Unexpected response validating the Groww connection.")
        except _GROWW_CALL_ERRORS as error:
            message = self._safe_message(error)
            if classify_failure(error) == FAILURE_AUTH:
                self._connection_status = "TOKEN_EXPIRED" if self._last_validated_at else "TOKEN_INVALID"
                self._client = None
            else:
                self._connection_status = "ERROR"
            self._last_error = message
            self._record_event("VALIDATION_FAILED", status="FAILED", error_message=message)
            self._persist()
            return ConnectionTestResult(False, message)
        now = datetime.now(IST)
        self._connection_status = "CONNECTED"
        self._last_validated_at = now
        self._last_successful_request_at = now
        self._last_error = None
        self._capabilities["profile"] = {"status": "AVAILABLE", "error": None, "checkedAt": now}
        self._record_event("VALIDATION_SUCCESS", status="SUCCESS")
        self._persist()
        return ConnectionTestResult(True, "Connected")

    def auto_refresh_if_needed(self) -> None:
        """Runs at startup (and may be called again later) to establish a
        working connection from whatever credentials are already known,
        with no user interaction. Reuses a stored session (access token)
        first if it still validates - avoiding an unnecessary re-mint - and
        otherwise mints a fresh session from the API key/secret via Groww's
        approval-checksum flow. Never raises - a failed
        auto-refresh just leaves connection_status reflecting why, for the
        API Management page to show."""
        if self._client is not None:
            return
        if self._access_token:
            try:
                self._activate(self._access_token)
                self._ensure_token_lifecycle_initialized()
                self._record_event(
                    "AUTO_REFRESH_SUCCESS", status="SUCCESS",
                    token_reference=credential_manager.reference_hint(self._access_token),
                )
                self._persist()
                return
            except _GROWW_CALL_ERRORS as error:
                self._last_error = self._safe_message(error)
                self._connection_status = (
                    ("TOKEN_EXPIRED" if self._last_validated_at else "TOKEN_INVALID")
                    if classify_failure(error) == FAILURE_AUTH else "ERROR"
                )
        if self._api_key and self._api_secret:
            try:
                token = self._mint_session(self._api_key, self._api_secret)
                self._set_fresh_token_lifecycle()
                self._record_event(
                    "AUTO_REFRESH_SUCCESS", status="SUCCESS", token_reference=credential_manager.reference_hint(token),
                )
                self._persist()
                return
            except _GROWW_CALL_ERRORS as error:
                self._last_error = self._safe_message(error)
                self._connection_status = "ERROR"
                self._record_event("AUTO_REFRESH_FAILED", status="FAILED", error_message=self._last_error)
        if not self._access_token and not (self._api_key and self._api_secret):
            self._connection_status = "MISSING"
        self._persist()
