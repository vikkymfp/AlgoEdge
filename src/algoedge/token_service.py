from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests
from growwapi import GrowwAPI
from growwapi.groww.exceptions import GrowwAPIAuthenticationException, GrowwAPIException

from algoedge import credential_manager, db
from algoedge.config import Settings
from algoedge.credential_manager import CredentialEncryptionUnavailable
from algoedge.risk_manager import IST

logger = logging.getLogger("algoedge.token_service")

BROKER_NAME = "groww"

# Exceptions that can surface from a real Groww call (auth failure, bad
# request, rate limit, timeout, or a genuine network/connectivity problem)
# or from growwapi's static get_access_token helper, which uses `requests`
# directly and isn't wrapped in GrowwAPIException on connection failures.
_GROWW_CALL_ERRORS = (GrowwAPIException, requests.RequestException, OSError, TypeError, ValueError, KeyError)


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
    access_token_masked: str | None
    token_status: str  # ACTIVE | EXPIRED | MISSING
    connection_status: str  # CONNECTED | TOKEN_EXPIRED | DISCONNECTED | MISSING | ERROR
    token_created_at: datetime | None
    token_expiry_at: datetime | None
    last_validated_at: datetime | None
    last_successful_request_at: datetime | None
    last_error: str | None
    credentials_persisted: bool


@dataclass(frozen=True)
class ConnectionTestResult:
    connected: bool
    message: str


class _TrackedClient:
    """Thin transparent proxy around a real GrowwAPI client that calls back
    into TokenService after any method call succeeds, so "Last Successful
    API Request" reflects actual usage across the whole app (Market Pulse's
    account calls, Manual Trading, grid/positions/orders panels) rather
    than only the handful of calls TokenService makes directly itself.
    Never swallows an exception - a failed call just doesn't mark success."""

    def __init__(self, client: GrowwAPI, on_success: Any) -> None:
        self._client = client
        self._on_success = on_success

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._client, name)
        if not callable(attribute):
            return attribute

        def _tracked(*args: Any, **kwargs: Any) -> Any:
            result = attribute(*args, **kwargs)
            self._on_success()
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
        self._token_created_at = row["tokenCreatedAt"]
        self._token_expiry_at = row["tokenExpiryAt"]
        self._last_validated_at = row["lastValidatedAt"]
        self._last_successful_request_at = row["lastSuccessfulRequestAt"]
        self._connection_status = row["connectionStatus"] or "MISSING"
        self._last_error = row["lastError"]

    # -- public read API ----------------------------------------------

    def is_connected(self) -> bool:
        return self._client is not None and self._connection_status == "CONNECTED"

    def effective_client(self) -> GrowwAPI:
        if self._client is None:
            raise BrokerNotConnectedError(
                self._last_error or "Groww is not connected. Configure it in API Management."
            )
        return _TrackedClient(self._client, self.mark_successful_request)  # type: ignore[return-value]

    def mark_successful_request(self) -> None:
        """Called by broker-facing code after any real Groww API call
        succeeds, so "Last Successful API Request" reflects actual usage,
        not just explicit Test Connection clicks. In-memory only - not
        persisted on every call, to avoid hammering the database for pure
        observability."""
        self._last_successful_request_at = datetime.now(IST)

    def status(self) -> BrokerStatus:
        has_any_credential = bool(self._access_token or (self._api_key and self._api_secret))
        if not has_any_credential:
            token_status = "MISSING"
        elif self._connection_status == "TOKEN_EXPIRED":
            token_status = "EXPIRED"
        else:
            token_status = "ACTIVE"
        return BrokerStatus(
            broker=BROKER_NAME,
            api_key_masked=credential_manager.mask_key(self._api_key),
            api_secret_masked=credential_manager.mask_secret(self._api_secret),
            access_token_masked=credential_manager.mask_secret(self._access_token),
            token_status=token_status,
            connection_status=self._connection_status,
            token_created_at=self._token_created_at,
            token_expiry_at=self._token_expiry_at,
            last_validated_at=self._last_validated_at,
            last_successful_request_at=self._last_successful_request_at,
            last_error=self._last_error,
            credentials_persisted=credential_manager.is_configured(self._settings.credential_encryption_key)
            and db.is_available(),
        )

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

    def _persist(self) -> None:
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
        db.save_broker_credential(BROKER_NAME, **fields)

    def _record_event(
        self, event: str, *, status: str, token_reference: str | None = None, error_message: str | None = None,
    ) -> None:
        db.record_token_audit_event(
            broker=BROKER_NAME, event=event, status=status,
            token_reference=token_reference, error_message=error_message,
        )

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
            self._connection_status = (
                "TOKEN_EXPIRED" if isinstance(error, GrowwAPIAuthenticationException) else "ERROR"
            )
        self._last_error = message
        return message

    # -- update workflows --------------------------------------------

    def update_access_token(self, raw_token: str) -> BrokerStatus:
        """The manual "Update Access Token" workflow: validates the token
        against a real, safe/read-only Groww endpoint BEFORE storing
        anything, per the required workflow order. Never invents a token -
        this only ever stores exactly what the caller supplied."""
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
        self._token_created_at = datetime.now(IST)
        self._token_expiry_at = None  # Groww does not publish a fixed access-token lifetime
        self._persist()
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
            token = GrowwAPI.get_access_token(api_key=api_key, secret=api_secret)
            self._activate(token)
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
        self._token_created_at = datetime.now(IST)
        self._token_expiry_at = None
        self._persist()
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
            self._connection_status = (
                "TOKEN_EXPIRED" if isinstance(error, GrowwAPIAuthenticationException) else "ERROR"
            )
            self._last_error = message
            self._record_event("VALIDATION_FAILED", status="FAILED", error_message=message)
            self._persist()
            return ConnectionTestResult(False, message)
        now = datetime.now(IST)
        self._connection_status = "CONNECTED"
        self._last_validated_at = now
        self._last_successful_request_at = now
        self._last_error = None
        self._record_event("VALIDATION_SUCCESS", status="SUCCESS")
        self._persist()
        return ConnectionTestResult(True, "Connected")

    def auto_refresh_if_needed(self) -> None:
        """Runs at startup (and may be called again later) to establish a
        working connection from whatever credentials are already known,
        with no user interaction. Tries a stored/env access token first,
        then falls back to minting a fresh one from an api key+secret pair
        via Groww's approval-checksum flow. Never raises - a failed
        auto-refresh just leaves connection_status reflecting why, for the
        API Management page to show."""
        if self._client is not None:
            return
        if self._access_token:
            try:
                self._activate(self._access_token)
                self._record_event(
                    "AUTO_REFRESH_SUCCESS", status="SUCCESS",
                    token_reference=credential_manager.reference_hint(self._access_token),
                )
                self._persist()
                return
            except _GROWW_CALL_ERRORS as error:
                self._last_error = self._safe_message(error)
                self._connection_status = (
                    "TOKEN_EXPIRED" if isinstance(error, GrowwAPIAuthenticationException) else "ERROR"
                )
        if self._api_key and self._api_secret:
            try:
                token = GrowwAPI.get_access_token(api_key=self._api_key, secret=self._api_secret)
                self._activate(token)
                self._token_created_at = datetime.now(IST)
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
