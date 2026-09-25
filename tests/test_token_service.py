import logging
from datetime import datetime, timedelta

import pytest
import requests
from growwapi.groww.exceptions import GrowwAPIAuthenticationException, GrowwAPITimeoutException

from algoedge import db as db_module
from algoedge import token_service as token_service_module
from algoedge.config import Settings
from algoedge.groww_broker import GrowwBroker
from algoedge.risk_manager import IST
from algoedge.token_service import BrokerNotConnectedError, BrokerValidationError, TokenService

REAL_SECRET = "totally-real-secret-value-should-never-leak"


@pytest.fixture(autouse=True)
def reset_db_module_state():
    db_module._engine = None
    db_module._session_factory = None
    yield
    db_module._engine = None
    db_module._session_factory = None


def make_settings(**overrides) -> Settings:
    """Explicit overrides for every credential/db field so tests never
    accidentally read this machine's real .env Groww credentials or touch
    its real SQL Server."""
    fields = {
        "groww_api_key": None,
        "groww_api_secret": None,
        "groww_access_token": None,
        "credential_encryption_key": "",
        "db_server": "",
    }
    fields.update(overrides)
    return Settings(**fields)


class FakeGrowwClient:
    def __init__(self, token: str, *, profile=None, profile_error: Exception | None = None) -> None:
        self.token = token
        self._profile = profile if profile is not None else {"user_id": "u1"}
        self._profile_error = profile_error

    def get_user_profile(self) -> dict:
        if self._profile_error is not None:
            raise self._profile_error
        return self._profile

    def place_order(self, **_kwargs) -> dict:
        return {"groww_order_id": "should-not-be-called-in-these-tests"}


def install_fake_groww(monkeypatch, *, profile_error=None, access_token_result=None, access_token_error=None):
    """Patches token_service.GrowwAPI with a fake whose behavior this test
    controls, so no real network call is ever made."""

    class FakeGrowwAPI:
        def __new__(cls, token):
            return FakeGrowwClient(token, profile_error=profile_error)

        @staticmethod
        def get_access_token(api_key, totp=None, secret=None):
            if access_token_error is not None:
                raise access_token_error
            return access_token_result or "generated-token"

    monkeypatch.setattr(token_service_module, "GrowwAPI", FakeGrowwAPI)


# -- missing credentials ----------------------------------------------


def test_missing_credentials_status_is_missing(monkeypatch) -> None:
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings())

    status = service.status()

    assert status.token_status == "UNAVAILABLE"
    assert service.is_connected() is False


def test_effective_client_raises_when_not_connected(monkeypatch) -> None:
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings())

    with pytest.raises(BrokerNotConnectedError):
        service.effective_client()


# -- invalid api key / secret -------------------------------------------


def test_invalid_api_key_or_secret_fails_credential_update(monkeypatch) -> None:
    install_fake_groww(monkeypatch, access_token_error=GrowwAPIAuthenticationException())
    service = TokenService(make_settings())

    with pytest.raises(BrokerValidationError):
        service.update_credentials("bad-key", "bad-secret")

    assert service.is_connected() is False
    # This credential pair was never successfully validated even once
    # (last_validated_at is still None) - Groww's fixed auth-failure
    # message doesn't distinguish "never valid" from "was valid, now
    # expired", so that distinction is made here from whether this app has
    # ever seen a successful validation for the current token/credentials.
    assert service.status().connection_status == "TOKEN_INVALID"
    assert service.status().token_status == "INVALID"


def test_failed_credential_update_does_not_disconnect_an_already_working_session(monkeypatch) -> None:
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings())
    service.update_access_token("a-good-token")
    assert service.is_connected() is True

    install_fake_groww(monkeypatch, access_token_error=GrowwAPIAuthenticationException())
    with pytest.raises(BrokerValidationError):
        service.update_credentials("bad-key", "bad-secret")

    # The bad *new* credentials must not tear down the still-good existing
    # session - only the update attempt itself failed.
    assert service.is_connected() is True
    assert service.status().connection_status == "CONNECTED"
    assert "expired" in (service.status().last_error or "").lower()


def test_invalid_credentials_does_not_persist_bad_state_as_connected(monkeypatch) -> None:
    install_fake_groww(monkeypatch, access_token_error=GrowwAPIAuthenticationException())
    service = TokenService(make_settings())

    with pytest.raises(BrokerValidationError):
        service.update_credentials("key", "secret")

    assert service.status().token_status != "ACTIVE" or service.status().connection_status != "CONNECTED"


# -- missing access token ------------------------------------------------


def test_update_access_token_rejects_empty_string(monkeypatch) -> None:
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings())

    with pytest.raises(ValueError, match="empty"):
        service.update_access_token("   ")


# -- expired token ---------------------------------------------------


def test_expired_token_is_reported_as_expired(monkeypatch) -> None:
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings(groww_access_token="a-token"))
    assert service.is_connected() is True  # activated fine at startup

    # Now the token has expired server-side - a later probe fails 401.
    monkeypatch.setattr(
        service, "_client",
        FakeGrowwClient("a-token", profile_error=GrowwAPIAuthenticationException()),
    )
    result = service.test_connection()

    assert result.connected is False
    assert service.status().token_status == "EXPIRED"
    assert service.status().connection_status == "TOKEN_EXPIRED"


# -- valid token -----------------------------------------------------


def test_valid_token_update_connects_successfully(monkeypatch) -> None:
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings())

    status = service.update_access_token("a-fresh-token")

    assert service.is_connected() is True
    assert status.token_status == "ACTIVE"
    assert status.connection_status == "CONNECTED"
    assert status.token_created_at is not None


# -- api timeout / groww unavailable -------------------------------------


def test_api_timeout_is_handled_without_raising_unexpected_error(monkeypatch) -> None:
    install_fake_groww(monkeypatch, profile_error=GrowwAPITimeoutException())
    service = TokenService(make_settings(groww_access_token="a-token"))

    result = service.test_connection()

    assert result.connected is False
    assert service.status().connection_status == "ERROR"


def test_groww_unavailable_network_error_is_handled(monkeypatch) -> None:
    install_fake_groww(monkeypatch, access_token_error=requests.exceptions.ConnectionError("no route to host"))
    service = TokenService(make_settings())

    with pytest.raises(BrokerValidationError):
        service.update_credentials("key", "secret")

    assert service.status().connection_status == "ERROR"


# -- successful / failed token update ------------------------------------


def test_successful_token_update_is_connected_and_recorded(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(
        db_module, "record_token_audit_event",
        lambda **kwargs: events.append(kwargs),
    )
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings())

    service.update_access_token("a-new-token")

    assert any(event["event"] == "TOKEN_UPDATED" and event["status"] == "SUCCESS" for event in events)


def test_failed_token_update_is_recorded_as_failed(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(
        db_module, "record_token_audit_event",
        lambda **kwargs: events.append(kwargs),
    )
    install_fake_groww(monkeypatch, profile_error=GrowwAPIAuthenticationException())
    service = TokenService(make_settings())

    with pytest.raises(BrokerValidationError):
        service.update_access_token("a-bad-token")

    assert any(event["event"] == "TOKEN_UPDATED" and event["status"] == "FAILED" for event in events)


# -- tokens never appear in logs -----------------------------------------


def test_raw_token_never_appears_in_audit_events(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(
        db_module, "record_token_audit_event",
        lambda **kwargs: events.append(kwargs),
    )
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings())

    service.update_access_token(REAL_SECRET)

    for event in events:
        for value in event.values():
            if isinstance(value, str):
                assert REAL_SECRET not in value


def test_raw_secret_never_appears_in_log_output(monkeypatch, caplog) -> None:
    install_fake_groww(monkeypatch, access_token_error=GrowwAPIAuthenticationException())
    with caplog.at_level(logging.DEBUG):
        service = TokenService(make_settings())
        try:
            service.update_credentials("some-key", REAL_SECRET)
        except BrokerValidationError:
            pass

    for record in caplog.records:
        assert REAL_SECRET not in record.getMessage()


def test_raw_token_never_appears_in_status_masked_fields(monkeypatch) -> None:
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings())

    service.update_access_token(REAL_SECRET)
    status = service.status()

    assert not hasattr(status, "access_token_masked")  # the session token is never displayed
    assert REAL_SECRET not in repr(status)
    assert REAL_SECRET not in (status.api_key_masked or "")
    assert REAL_SECRET not in (status.api_secret_masked or "")


# -- trading blocked when authentication is invalid ----------------------


def test_broker_execute_blocked_when_not_connected(monkeypatch) -> None:
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings())
    settings = make_settings(live_trading=True)
    broker = GrowwBroker(settings=settings, token_service=service)

    with pytest.raises(BrokerNotConnectedError):
        broker.execute("buy", 100.0, quantity=1)


def test_broker_execute_allowed_once_connected(monkeypatch) -> None:
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings())
    service.update_access_token("a-good-token")
    settings = make_settings(live_trading=True)
    broker = GrowwBroker(settings=settings, token_service=service)

    result = broker.execute("buy", 100.0, quantity=1)

    assert result["groww_order_id"]


# -- valid / expiring-soon / expired (estimated clock) --------------------


def test_legacy_token_created_before_this_feature_gets_its_expiry_backfilled(monkeypatch) -> None:
    # A token persisted before token_expiry_at existed has a real
    # token_created_at but a null token_expiry_at (the old code always
    # stored None there) - re-validating it at startup must backfill the
    # estimate from the existing creation time, not leave it permanently
    # null just because the token itself wasn't freshly minted this run.
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings(groww_access_token="a-legacy-token"))
    service._token_created_at = datetime(2026, 9, 24, 7, 57, 27, tzinfo=IST)
    service._token_expiry_at = None

    # auto_refresh_if_needed() only runs once automatically (at __init__,
    # before the legacy fields above were injected) and short-circuits
    # while a client is already connected - call it directly after forcing
    # _client back to None to simulate "app restarts, re-loads this same
    # legacy record, re-validates the same token".
    service._client = None
    service.auto_refresh_if_needed()

    status = service.status()
    assert status.token_expiry_at is not None
    assert status.token_created_at == datetime(2026, 9, 24, 7, 57, 27, tzinfo=IST)  # unchanged, not reset to now


def test_valid_token_reports_active_with_an_expiry_estimate(monkeypatch) -> None:
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings())

    status = service.update_access_token("a-fresh-token")

    assert status.token_status == "ACTIVE"
    assert status.connection_status == "CONNECTED"
    assert status.token_expiry_at is not None
    assert status.token_expiry_is_estimated is True
    assert status.token_expiry_at > datetime.now(IST)


def test_token_close_to_estimated_daily_reset_reports_expiring_soon(monkeypatch) -> None:
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings())
    service.update_access_token("a-token")

    # Simulate the estimated ~6am IST daily reset being 10 minutes away.
    service._token_expiry_at = datetime.now(IST) + timedelta(minutes=10)

    status = service.status()
    assert status.token_status == "EXPIRING_SOON"
    # The estimate only ever adds an early warning - it never touches the
    # evidence-based connection_status on its own.
    assert status.connection_status == "CONNECTED"


def test_token_past_estimated_expiry_reports_expired_even_before_a_call_fails(monkeypatch) -> None:
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings())
    service.update_access_token("a-token")

    service._token_expiry_at = datetime.now(IST) - timedelta(minutes=1)

    status = service.status()
    assert status.token_status == "EXPIRED"
    assert status.connection_status == "CONNECTED"  # real evidence unchanged until a real call actually fails


# -- invalid token (never valid, distinct from expired) --------------------


def test_invalid_token_is_reported_as_invalid_not_expired(monkeypatch) -> None:
    install_fake_groww(monkeypatch, profile_error=GrowwAPIAuthenticationException())
    service = TokenService(make_settings())

    with pytest.raises(BrokerValidationError):
        service.update_access_token("a-token-that-was-never-valid")

    status = service.status()
    assert status.connection_status == "TOKEN_INVALID"
    assert status.token_status == "INVALID"
    assert service.is_connected() is False


# -- failed API request marks the connection live, not just Test Connection -----


def test_a_failed_request_anywhere_in_the_app_marks_the_connection_expired(monkeypatch) -> None:
    """The core fix: any real call made through effective_client() - not
    just an explicit Test Connection click - must immediately downgrade
    connection_status the moment it fails. This is what keeps "Groww
    Connected" live instead of stale."""
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings())
    service.update_access_token("a-good-token")
    assert service.is_connected() is True

    tracked_client = service.effective_client()
    service._client._profile_error = GrowwAPIAuthenticationException()
    with pytest.raises(GrowwAPIAuthenticationException):
        tracked_client.get_user_profile()

    assert service.is_connected() is False
    status = service.status()
    assert status.connection_status == "TOKEN_EXPIRED"
    assert status.token_status == "EXPIRED"
    # effective_client() must now refuse to hand out a client built from a
    # token already confirmed bad, rather than let a caller keep using it.
    with pytest.raises(BrokerNotConnectedError):
        service.effective_client()


def test_a_non_auth_failure_marks_error_but_keeps_the_client_usable(monkeypatch) -> None:
    """A transient network/timeout blip doesn't indict the token itself -
    forcing a full reconnect over a temporary hiccup would be overkill."""
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings())
    service.update_access_token("a-token")

    service._client._profile_error = GrowwAPITimeoutException()
    with pytest.raises(GrowwAPITimeoutException):
        service.effective_client().get_user_profile()

    assert service.status().connection_status == "ERROR"
    assert service._client is not None  # kept, not discarded


def test_repeated_failures_only_record_one_audit_event_per_transition(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(db_module, "record_token_audit_event", lambda **kwargs: events.append(kwargs))
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings())
    service.update_access_token("a-good-token")
    events.clear()

    service._client._profile_error = GrowwAPIAuthenticationException()
    for _ in range(3):
        try:
            service.effective_client().get_user_profile()
        except (GrowwAPIAuthenticationException, BrokerNotConnectedError):
            pass

    connection_lost_events = [event for event in events if event["event"] == "CONNECTION_LOST"]
    assert len(connection_lost_events) == 1


# -- successful revalidation -----------------------------------------------


def test_successful_revalidation_restores_connected_after_a_failure(monkeypatch) -> None:
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings())
    service.update_access_token("a-token")
    service._client._profile_error = GrowwAPIAuthenticationException()
    with pytest.raises(GrowwAPIAuthenticationException):
        service.effective_client().get_user_profile()
    assert service.is_connected() is False

    install_fake_groww(monkeypatch)  # a fresh, working fake for the new token
    status = service.update_access_token("a-fresh-valid-token")

    assert service.is_connected() is True
    assert status.connection_status == "CONNECTED"
    assert status.token_status == "ACTIVE"
    assert status.last_error is None


def test_test_connection_button_also_restores_connected_after_a_failure(monkeypatch) -> None:
    install_fake_groww(monkeypatch)
    service = TokenService(make_settings())
    service.update_access_token("a-token")
    service._client._profile_error = GrowwAPIAuthenticationException()
    with pytest.raises(GrowwAPIAuthenticationException):
        service.effective_client().get_user_profile()
    assert service.is_connected() is False

    # The same underlying client object recovers (e.g. Groww's side had a
    # transient hiccup) - re-running Test Connection re-validates it fresh.
    service._client = token_service_module.GrowwAPI("a-token")
    result = service.test_connection()

    assert result.connected is True
    assert service.is_connected() is True
    assert service.status().connection_status == "CONNECTED"
