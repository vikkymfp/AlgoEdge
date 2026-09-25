"""Groww credential model: the API key + secret are the persistent
credentials; the access token is session state minted from them
(GrowwAPI.get_access_token(api_key, secret=...)) and renewed by
TokenService when it expires. Covers key/secret -> session -> expiry ->
re-authentication, and that the session token is never shown as a
credential."""

from datetime import datetime, timedelta

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
from algoedge.risk_manager import IST
from algoedge.token_service import (
    AUTH_MODE_API_KEY_SECRET,
    AUTH_MODE_MANUAL_TOKEN,
    BrokerNotConnectedError,
    BrokerValidationError,
    TokenService,
)

ENCRYPTION_KEY = Fernet.generate_key().decode()
API_KEY = "gw-api-key-ABCD"
API_SECRET = "api-secret-value-never-shown"


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


class FakeGroww:
    """Stands in for growwapi: get_access_token mints session-1,
    session-2, ...; a session listed in `expired` fails every call with a
    401, as a real expired Groww session does."""

    def __init__(self) -> None:
        self.minted: list[str] = []
        self.mint_error: Exception | None = None
        self.expired: set[str] = set()
        self.orders_sent: list[str] = []

    def install(self, monkeypatch) -> None:
        fake = self

        class FakeClient:
            def __init__(self, token: str) -> None:
                self.token = token

            def _check(self) -> None:
                if self.token in fake.expired:
                    raise GrowwAPIAuthenticationException()

            def get_user_profile(self) -> dict:
                self._check()
                return {"user_id": "u1"}

            def get_positions_for_user(self, segment=None) -> dict:
                self._check()
                return {"positions": []}

            def place_order(self, **_kwargs) -> dict:
                fake.orders_sent.append(self.token)
                self._check()
                return {"groww_order_id": "o1"}

        class FakeGrowwAPI:
            def __new__(cls, token):
                return FakeClient(token)

            @staticmethod
            def get_access_token(api_key, totp=None, secret=None):
                assert (api_key, secret) == (API_KEY, API_SECRET)
                if fake.mint_error is not None:
                    raise fake.mint_error
                token = f"session-{len(fake.minted) + 1}"
                fake.minted.append(token)
                return token

        monkeypatch.setattr(token_service_module, "GrowwAPI", FakeGrowwAPI)


@pytest.fixture
def groww(monkeypatch) -> FakeGroww:
    fake = FakeGroww()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def events(monkeypatch) -> list[dict]:
    recorded: list[dict] = []
    real = db_module.record_token_audit_event

    def record(**kwargs):
        recorded.append(kwargs)
        real(**kwargs)

    monkeypatch.setattr(db_module, "record_token_audit_event", record)
    return recorded


def key_secret_service(**overrides) -> TokenService:
    return TokenService(make_settings(groww_api_key=API_KEY, groww_api_secret=API_SECRET, **overrides))


def current_token(service: TokenService) -> str:
    return service.effective_client().token


def expire_current_session(service: TokenService, groww: FakeGroww) -> None:
    groww.expired.add(service._client.token)
    with pytest.raises(GrowwAPIAuthenticationException):
        service.effective_client().get_positions_for_user()


# -- key/secret -> session ---------------------------------------------------


def test_api_key_and_secret_generate_the_session_at_startup(groww) -> None:
    service = key_secret_service()

    assert groww.minted == ["session-1"]
    assert current_token(service) == "session-1"
    status = service.status()
    assert status.connection_status == "CONNECTED"
    assert status.token_status == "ACTIVE"
    assert status.auth_mode == AUTH_MODE_API_KEY_SECRET
    assert status.auto_reauth_available is True
    assert status.token_created_at is not None
    assert status.token_expiry_at is not None


def test_the_session_token_is_never_exposed_as_a_credential(groww, monkeypatch) -> None:
    service = key_secret_service()
    monkeypatch.setattr(web_server, "token_service", service)

    payload = web_server.broker_status()

    assert not any("accesstoken" in key.lower() for key in payload)
    assert "session-1" not in repr(payload)
    assert API_SECRET not in repr(payload)
    assert payload["authMode"] == AUTH_MODE_API_KEY_SECRET
    assert payload["sessionStatus"] == "ACTIVE"


# -- session expiry -> re-authentication ------------------------------------


def test_expired_session_is_regenerated_from_key_and_secret(groww, events) -> None:
    service = key_secret_service()
    expire_current_session(service, groww)
    assert service.current_connection_status() == "TOKEN_EXPIRED"
    assert service.status().token_status == "EXPIRED"
    # The key/secret themselves are untouched by a session expiring.
    assert service.status().api_key_masked == "gw...ABCD"

    assert current_token(service) == "session-2"

    assert groww.minted == ["session-1", "session-2"]
    assert service.current_connection_status() == "CONNECTED"
    assert service.status().token_status == "ACTIVE"
    assert any(event["event"] == "SESSION_REAUTHENTICATED" for event in events)


def test_the_call_that_hit_the_expired_session_is_never_retried(groww) -> None:
    service = key_secret_service()
    groww.expired.add("session-1")

    with pytest.raises(GrowwAPIAuthenticationException):
        service.effective_client().place_order(trading_symbol="X")

    # Sent once, on the dead session - never silently re-sent on the new one.
    assert groww.orders_sent == ["session-1"]
    service.effective_client()
    assert groww.orders_sent == ["session-1"]


def test_session_past_its_estimated_daily_reset_is_renewed_proactively(groww) -> None:
    service = key_secret_service()
    service._token_expiry_at = datetime.now(IST) - timedelta(minutes=1)

    assert current_token(service) == "session-2"
    assert service._token_expiry_at > datetime.now(IST)


def test_failed_proactive_renewal_keeps_the_working_session(groww, events) -> None:
    service = key_secret_service()
    service._token_expiry_at = datetime.now(IST) - timedelta(minutes=1)
    groww.mint_error = GrowwAPIException(code="400", msg="Groww API Error 400: approval pending")

    assert current_token(service) == "session-1"
    assert service.current_connection_status() == "CONNECTED"
    assert service.status().last_error is None
    assert [event["event"] for event in events if event["event"] == "REAUTH_FAILED"] == ["REAUTH_FAILED"]


# -- re-authentication failure, cooldown, retry -------------------------------


def test_failed_reauthentication_stays_expired_and_is_rate_limited(groww, events) -> None:
    service = key_secret_service()
    expire_current_session(service, groww)
    groww.mint_error = GrowwAPIException(code="400", msg="Groww API Error 400: approval pending")

    for _ in range(3):
        with pytest.raises(BrokerNotConnectedError, match="Re-authentication failed"):
            service.effective_client()

    assert groww.minted == ["session-1"]
    assert service.current_connection_status() == "TOKEN_EXPIRED"
    # One attempt inside the cooldown, and one audit entry for the streak.
    assert len([event for event in events if event["event"] == "REAUTH_FAILED"]) == 1

    groww.mint_error = None
    service._last_reauth_attempt_at -= timedelta(seconds=token_service_module._REAUTH_COOLDOWN_SECONDS)

    assert current_token(service) == "session-2"
    assert service.current_connection_status() == "CONNECTED"


def test_explicit_reauthenticate_ignores_the_cooldown(groww, monkeypatch) -> None:
    service = key_secret_service()
    monkeypatch.setattr(web_server, "token_service", service)
    expire_current_session(service, groww)
    groww.mint_error = GrowwAPIException(code="400", msg="approval pending")
    with pytest.raises(BrokerNotConnectedError):
        service.effective_client()
    groww.mint_error = None

    payload = web_server.broker_reauthenticate()  # e.g. right after approving in the Groww app

    assert payload["connectionStatus"] == "CONNECTED"
    assert groww.minted == ["session-1", "session-2"]


def test_explicit_reauthenticate_reports_failure(groww) -> None:
    service = key_secret_service()
    groww.mint_error = GrowwAPIException(code="400", msg="approval pending")

    with pytest.raises(BrokerValidationError, match="approval pending"):
        service.reauthenticate()
    assert service.is_connected() is True  # the existing session is kept


# -- manually supplied session token (no key/secret) --------------------------


def test_pasted_session_token_is_not_renewed_automatically(groww) -> None:
    service = TokenService(make_settings(groww_access_token="pasted-session"))
    assert service.status().auth_mode == AUTH_MODE_MANUAL_TOKEN
    assert service.status().auto_reauth_available is False

    expire_current_session(service, groww)

    with pytest.raises(BrokerNotConnectedError):
        service.effective_client()
    assert groww.minted == []
    assert service.current_connection_status() == "TOKEN_EXPIRED"
    with pytest.raises(ValueError, match="API key and secret"):
        service.reauthenticate()


def test_key_and_secret_take_over_renewal_from_a_pasted_session(groww) -> None:
    service = key_secret_service(groww_access_token="pasted-session")
    assert current_token(service) == "pasted-session"  # a still-valid session is reused

    expire_current_session(service, groww)

    assert current_token(service) == "session-1"


# -- restart: persistent key/secret, session reused or regenerated -----------


def test_restart_reuses_a_valid_stored_session(groww, sqlite_db) -> None:
    TokenService(make_settings(credential_encryption_key=ENCRYPTION_KEY)).update_credentials(API_KEY, API_SECRET)

    restarted = TokenService(make_settings(credential_encryption_key=ENCRYPTION_KEY))

    assert current_token(restarted) == "session-1"
    assert groww.minted == ["session-1"]
    assert restarted.status().auth_mode == AUTH_MODE_API_KEY_SECRET
    assert restarted.status().token_status == "ACTIVE"


def test_restart_regenerates_an_expired_stored_session_from_stored_key_and_secret(groww, sqlite_db) -> None:
    TokenService(make_settings(credential_encryption_key=ENCRYPTION_KEY)).update_credentials(API_KEY, API_SECRET)
    groww.expired.add("session-1")  # e.g. the app restarted after Groww's daily reset

    restarted = TokenService(make_settings(credential_encryption_key=ENCRYPTION_KEY))

    assert current_token(restarted) == "session-2"
    assert restarted.current_connection_status() == "CONNECTED"


# -- fix 1: fno_signals --live renews its session for the whole run -----------


def fno_live_session(monkeypatch):
    from fno_signals import broker as fno_broker_module

    monkeypatch.setattr(
        fno_broker_module, "get_settings",
        lambda: make_settings(groww_api_key=API_KEY, groww_api_secret=API_SECRET),
    )
    return fno_broker_module.generate_daily_session()


def resolved_contract():
    from datetime import date

    from fno_signals.broker import ResolvedContract

    return ResolvedContract(
        trading_symbol="NIFTY26SEP23200PE", exchange="NSE", expiry_date=date(2026, 9, 29),
        strike=23200, right="PE", lot_size=75,
    )


def test_fno_live_regenerates_an_expired_session_mid_run(groww, monkeypatch) -> None:
    client = fno_live_session(monkeypatch)
    assert client.token == "session-1"

    groww.expired.add("session-1")  # Groww's daily reset, mid-run
    with pytest.raises(GrowwAPIAuthenticationException):
        client.get_positions_for_user(segment="FNO")

    # The next broker operation gets a fresh session - no restart needed.
    assert client.get_positions_for_user(segment="FNO") == {"positions": []}
    assert client.token == "session-2"
    assert groww.minted == ["session-1", "session-2"]


def test_fno_live_order_on_an_expired_session_fails_once_and_is_not_retried(groww, monkeypatch) -> None:
    from fno_signals.broker import execute_market_order

    client = fno_live_session(monkeypatch)
    groww.expired.add("session-1")

    with pytest.raises(GrowwAPIAuthenticationException):
        execute_market_order(client, resolved_contract(), quantity=75)
    client.get_positions_for_user(segment="FNO")  # later operations renew the session...

    assert groww.orders_sent == ["session-1"]  # ...but the failed order is never re-sent


def test_fno_live_order_is_not_sent_when_no_session_can_be_obtained(groww, monkeypatch) -> None:
    from fno_signals.broker import GrowwSessionUnavailableError, execute_market_order

    client = fno_live_session(monkeypatch)
    groww.expired.add("session-1")
    with pytest.raises(GrowwAPIAuthenticationException):
        client.get_positions_for_user(segment="FNO")
    groww.mint_error = GrowwAPIException(code="400", msg="Groww API Error 400: approval pending")
    groww.orders_sent.clear()

    # A GrowwAPIException, so the live flow's existing handler marks the
    # order failed exactly as for any other failed Groww call.
    with pytest.raises(GrowwAPIException) as raised:
        execute_market_order(client, resolved_contract(), quantity=75)

    assert isinstance(raised.value, GrowwSessionUnavailableError)
    assert "approval pending" in str(raised.value)
    assert groww.orders_sent == []


# -- fix 2: manual order / GrowwBroker.execute renew before gating ------------


def live_broker(service: TokenService):
    from algoedge.groww_broker import GrowwBroker

    return GrowwBroker(settings=make_settings(live_trading=True), token_service=service)


def test_broker_execute_renews_an_expired_session_then_places_once(groww) -> None:
    service = key_secret_service()
    expire_current_session(service, groww)
    assert service.is_connected() is False  # the stale status that used to reject the order

    live_broker(service).execute("buy", price=100.0)

    assert groww.orders_sent == ["session-2"]


def test_broker_execute_is_blocked_by_a_genuine_authentication_failure(groww) -> None:
    service = key_secret_service()
    expire_current_session(service, groww)
    groww.mint_error = GrowwAPIAuthenticationException()

    with pytest.raises(BrokerNotConnectedError):
        live_broker(service).execute("buy", price=100.0)

    assert groww.orders_sent == []


def test_broker_execute_still_blocks_on_a_transient_error(groww) -> None:
    from growwapi.groww.exceptions import GrowwAPITimeoutException

    service = key_secret_service()
    service._client.get_positions_for_user = lambda segment=None: (_ for _ in ()).throw(GrowwAPITimeoutException())
    with pytest.raises(GrowwAPITimeoutException):
        service.effective_client().get_positions_for_user()

    with pytest.raises(BrokerNotConnectedError):
        live_broker(service).execute("buy", price=100.0)
    assert groww.orders_sent == []


class _PassedConnectionGate(Exception):
    pass


@pytest.fixture
def manual_order_endpoint(monkeypatch):
    """Runs the real endpoint up to its connection gate: reaching the next
    step (the reconciliation check) raises _PassedConnectionGate."""
    monkeypatch.setattr(web_server.settings, "live_trading", True)

    def reached_next_step():
        raise _PassedConnectionGate

    monkeypatch.setattr(web_server, "_run_reconciliation_check", reached_next_step)

    def place():
        return web_server.manual_trading_place_order(index_id="nifty-50", expiry="2026-09-29", strike=23200, right="PE")

    return place


def test_manual_order_renews_an_expired_session_instead_of_rejecting(groww, monkeypatch, manual_order_endpoint) -> None:
    service = key_secret_service()
    monkeypatch.setattr(web_server, "token_service", service)
    expire_current_session(service, groww)

    with pytest.raises(_PassedConnectionGate):
        manual_order_endpoint()
    assert service.current_connection_status() == "CONNECTED"
    assert groww.minted == ["session-1", "session-2"]


def test_manual_order_is_blocked_by_a_genuine_authentication_failure(groww, monkeypatch, manual_order_endpoint) -> None:
    from fastapi import HTTPException

    service = key_secret_service()
    monkeypatch.setattr(web_server, "token_service", service)
    expire_current_session(service, groww)
    groww.mint_error = GrowwAPIAuthenticationException()

    with pytest.raises(HTTPException) as raised:
        manual_order_endpoint()

    assert raised.value.status_code == 503
    assert groww.orders_sent == []


# -- fix 3: RENEWAL_DUE vs a real expiry --------------------------------------


def test_past_estimated_reset_with_a_working_session_is_renewal_due(groww, monkeypatch) -> None:
    service = key_secret_service()
    monkeypatch.setattr(web_server, "token_service", service)
    groww.mint_error = GrowwAPIException(code="400", msg="approval pending")
    service._token_expiry_at = datetime.now(IST) - timedelta(minutes=5)
    service.effective_client()  # renewal attempted and failed; the session still works

    payload = web_server.broker_status()
    assert payload["connectionStatus"] == "CONNECTED"
    assert payload["sessionStatus"] == "RENEWAL_DUE"
    assert web_server.system_health()["broker"]["status"] == "CONNECTED"

    # Only a real authentication failure makes it expired.
    expire_current_session(service, groww)
    payload = web_server.broker_status()
    assert payload["connectionStatus"] == "TOKEN_EXPIRED"
    assert payload["sessionStatus"] == "EXPIRED"
