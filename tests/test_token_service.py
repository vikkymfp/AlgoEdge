import logging

import pytest
import requests
from growwapi.groww.exceptions import GrowwAPIAuthenticationException, GrowwAPITimeoutException

from algoedge import db as db_module
from algoedge import token_service as token_service_module
from algoedge.config import Settings
from algoedge.groww_broker import GrowwBroker
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

    assert status.token_status == "MISSING"
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
    # Groww's own auth exception covers both "invalid" and "expired" cases
    # with one fixed message, so this app doesn't try to guess which -
    # matches the same TOKEN_EXPIRED bucket a real expiry would land in.
    assert service.status().connection_status == "TOKEN_EXPIRED"


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

    assert REAL_SECRET not in (status.access_token_masked or "")
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
