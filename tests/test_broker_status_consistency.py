"""The result of a credential update and the current broker connection
state are separate things: TOKEN_UPDATED SUCCESS only says the update
worked, and every view (header, API Management, Diagnostics, System
Health) must read the one current state from TokenService."""

import pytest
from cryptography.fernet import Fernet
from growwapi.groww.exceptions import GrowwAPIAuthenticationException, GrowwAPIException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from algoedge import db as db_module
from algoedge import token_service as token_service_module
from algoedge import web_server
from algoedge.config import Settings
from algoedge.models import Base
from algoedge.token_service import TokenService

ENCRYPTION_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def reset_db_module_state():
    db_module._engine = None
    db_module._session_factory = None
    yield
    db_module._engine = None
    db_module._session_factory = None


@pytest.fixture
def sqlite_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db_module._engine = engine
    db_module._session_factory = sessionmaker(bind=engine)


def make_settings(**overrides) -> Settings:
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
    def __init__(self, token: str) -> None:
        self.token = token
        self.profile_error: Exception | None = None

    def get_user_profile(self) -> dict:
        if self.profile_error is not None:
            raise self.profile_error
        return {"user_id": "u1"}


@pytest.fixture
def fake_groww(monkeypatch):
    class FakeGrowwAPI:
        def __new__(cls, token):
            return FakeGrowwClient(token)

        @staticmethod
        def get_access_token(api_key, totp=None, secret=None):
            return "minted-token"

    monkeypatch.setattr(token_service_module, "GrowwAPI", FakeGrowwAPI)


@pytest.fixture
def web_service(monkeypatch, fake_groww):
    """Swaps the web app's module-level TokenService for a test one."""
    service = TokenService(make_settings(credential_encryption_key=ENCRYPTION_KEY))
    monkeypatch.setattr(web_server, "token_service", service)
    return service


def fail_next_call(service: TokenService, error: Exception) -> None:
    service._client.profile_error = error
    with pytest.raises(type(error)):
        service.effective_client().get_user_profile()


def all_views_connection_status() -> set[str]:
    """What the header/API Management/Diagnostics (all fed by
    /api/broker/status) and System Health each report right now."""
    return {web_server.broker_status()["connectionStatus"], web_server.system_health()["broker"]["status"]}


# -- successful token save followed by connection failure -----------------


def test_token_save_then_forbidden_error_reports_error_not_connected(sqlite_db, web_service) -> None:
    payload = web_server.broker_update_access_token(web_server.AccessTokenUpdateRequest(accessToken="good-token"))
    assert payload["update"] == {"persisted": True}
    assert payload["connectionStatus"] == "CONNECTED"

    # The user's reported state: a later real Groww call is refused 403.
    fail_next_call(web_service, GrowwAPIException(code="403", msg="Access forbidden for this request"))

    status = web_server.broker_status()
    assert status["connectionStatus"] == "ERROR"
    assert status["tokenStatus"] == "UNAVAILABLE"
    assert status["lastError"] == "Access forbidden for this request"
    assert status["manualTradingBlocked"] is True
    assert all_views_connection_status() == {"ERROR"}
    # History still records that the update itself succeeded - that is
    # about the operation, and must not be read as "currently connected".
    events = {(event["event"], event["status"]) for event in web_server.broker_history()["events"]}
    assert ("TOKEN_UPDATED", "SUCCESS") in events
    assert ("CONNECTION_LOST", "FAILED") in events


def test_credentials_save_reports_persisted_separately_from_connection(sqlite_db, web_service) -> None:
    payload = web_server.broker_update_credentials(
        web_server.CredentialsUpdateRequest(apiKey="key-1234", apiSecret="secret-value"),
    )

    assert payload["update"] == {"persisted": True}
    assert payload["connectionStatus"] == "CONNECTED"


def test_update_without_encryption_key_is_not_reported_as_persisted(sqlite_db, monkeypatch, fake_groww) -> None:
    service = TokenService(make_settings())
    monkeypatch.setattr(web_server, "token_service", service)

    payload = web_server.broker_update_access_token(web_server.AccessTokenUpdateRequest(accessToken="good-token"))

    assert payload["update"] == {"persisted": False}


def test_update_without_database_is_not_reported_as_persisted(web_service) -> None:
    payload = web_server.broker_update_access_token(web_server.AccessTokenUpdateRequest(accessToken="good-token"))

    assert payload["update"] == {"persisted": False}


# -- connection loss after successful validation --------------------------


def test_connection_loss_after_successful_validation_updates_every_view(web_service) -> None:
    web_service.update_access_token("good-token")
    assert web_service.test_connection().connected is True
    assert all_views_connection_status() == {"CONNECTED"}

    fail_next_call(web_service, GrowwAPIAuthenticationException())

    assert all_views_connection_status() == {"TOKEN_EXPIRED"}
    assert web_server.broker_status()["tokenStatus"] == "EXPIRED"
    # Historical only: the earlier success is kept, but it doesn't make the
    # connection current.
    assert web_server.broker_status()["lastSuccessfulRequestAt"] is not None


def test_stored_connected_without_a_live_client_is_reported_disconnected(web_service) -> None:
    web_service.update_access_token("good-token")
    web_service._client = None

    assert web_service.current_connection_status() == "DISCONNECTED"
    assert web_service.is_connected() is False
    assert web_service.status().token_status == "UNAVAILABLE"
    assert all_views_connection_status() == {"DISCONNECTED"}
