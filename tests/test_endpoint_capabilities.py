"""A Groww failure is classified before it touches the broker connection:

- AUTH (expired/invalid token, session failure) -> TOKEN_EXPIRED /
  TOKEN_INVALID, client discarded - the existing safety behavior.
- CAPABILITY (403/404, a specific endpoint or feature not permitted) ->
  only that capability is UNAVAILABLE; the broker stays CONNECTED.
- TRANSIENT (timeout, network, unexpected) -> ERROR, client kept, and the
  next successful call restores CONNECTED.
"""

import pandas as pd
import pytest
from growwapi.groww.exceptions import (
    GrowwAPIAuthenticationException,
    GrowwAPIAuthorisationException,
    GrowwAPIException,
    GrowwAPINotFoundException,
    GrowwAPIRateLimitException,
    GrowwAPITimeoutException,
)

from algoedge import db as db_module
from algoedge import token_service as token_service_module
from algoedge import web_server
from algoedge.config import Settings
from algoedge.groww_broker import GrowwBroker
from algoedge.live_grid import LiveGridService
from algoedge.token_service import (
    FAILURE_AUTH,
    FAILURE_CAPABILITY,
    FAILURE_TRANSIENT,
    BrokerNotConnectedError,
    TokenService,
    classify_failure,
)
from fno_signals import broker as fno_broker_module

FORBIDDEN = "Access forbidden for this request"


@pytest.fixture(autouse=True)
def reset_state():
    db_module._engine = None
    db_module._session_factory = None
    fno_broker_module._instruments_cache.clear()
    yield
    db_module._engine = None
    db_module._session_factory = None
    fno_broker_module._instruments_cache.clear()


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
    """Every endpoint succeeds unless the test puts an exception in
    `failures` for that method name."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.failures: dict[str, Exception] = {}

    def _call(self, name: str, result):
        if name in self.failures:
            raise self.failures[name]
        return result

    def get_user_profile(self):
        return self._call("get_user_profile", {"user_id": "u1", "active_segments": ["CASH"]})

    def get_holdings_for_user(self):
        return self._call("get_holdings_for_user", {"holdings": []})

    def get_positions_for_user(self, segment=None):
        return self._call("get_positions_for_user", {"positions": []})

    def get_available_margin_details(self):
        return self._call("get_available_margin_details", {"clear_cash": 1000})

    def get_order_list(self, segment=None, page=0, page_size=25):
        return self._call("get_order_list", {"order_list": []})

    def get_quote(self, trading_symbol, exchange, segment):
        return self._call("get_quote", {"ltp": 100.0})

    def get_all_instruments(self):
        return self._call("get_all_instruments", pd.DataFrame([{"trading_symbol": "NIFTY"}]))


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
def service(fake_groww) -> TokenService:
    token_service = TokenService(make_settings())
    token_service.update_access_token("good-token")
    assert token_service.is_connected() is True
    return token_service


@pytest.fixture
def web_app(monkeypatch, service):
    """Points the web app's module-level services at the test TokenService,
    so the header/Broker page (/api/broker/status), Diagnostics
    (/api/account) and System Health all read the same state."""
    settings = make_settings()
    live_grid = LiveGridService(GrowwBroker(settings=settings, token_service=service), settings)
    monkeypatch.setattr(web_server, "token_service", service)
    monkeypatch.setattr(web_server, "service", live_grid)
    return live_grid


def call(service: TokenService, method: str, *args, **kwargs):
    return getattr(service.effective_client(), method)(*args, **kwargs)


def fail(service: TokenService, method: str, error: Exception, *args, **kwargs) -> None:
    service._client.failures[method] = error
    with pytest.raises(type(error)):
        call(service, method, *args, **kwargs)


def views() -> set[str]:
    return {web_server.broker_status()["connectionStatus"], web_server.system_health()["broker"]["status"]}


# -- classification ---------------------------------------------------------


@pytest.mark.parametrize(("error", "expected"), [
    (GrowwAPIAuthenticationException(), FAILURE_AUTH),
    (GrowwAPIException(code="401", msg="Request failed"), FAILURE_AUTH),
    (GrowwAPIException(code="GA001", msg="Token expired, please login again"), FAILURE_AUTH),
    (GrowwAPIException(code="GA001", msg="Invalid token"), FAILURE_AUTH),
    # Ambiguous: mentions both - must land on the safe (auth) side.
    (GrowwAPIException(code="403", msg="Forbidden: session expired"), FAILURE_AUTH),
    (GrowwAPIAuthorisationException(), FAILURE_CAPABILITY),
    (GrowwAPINotFoundException(), FAILURE_CAPABILITY),
    (GrowwAPIException(code="403", msg="Request failed"), FAILURE_CAPABILITY),
    (GrowwAPIException(code="GA403", msg=FORBIDDEN), FAILURE_CAPABILITY),
    (GrowwAPIException(code="GA009", msg="Live data not subscribed"), FAILURE_CAPABILITY),
    (GrowwAPITimeoutException(), FAILURE_TRANSIENT),
    (GrowwAPIRateLimitException(), FAILURE_TRANSIENT),
    (GrowwAPIException(code="500", msg="The request to the Groww API failed."), FAILURE_TRANSIENT),
    (OSError("connection reset"), FAILURE_TRANSIENT),
])
def test_classify_failure(error, expected) -> None:
    assert classify_failure(error) == expected


# -- 1. valid token + endpoint 403 ------------------------------------------


def test_the_reported_example_market_data_403_keeps_broker_connected(web_app, service) -> None:
    """Profile OK + Positions OK + Orders OK + Market Data 403."""
    service._client.failures["get_quote"] = GrowwAPIException(code="GA403", msg=FORBIDDEN)
    web_app.snapshot()  # the Positions grid's real get_quote call - swallowed there, tracked here

    account = web_server.account()
    assert account["profile"]["connected"] is True
    assert account["positionsStatus"]["available"] is True
    assert account["ordersStatus"]["available"] is True
    assert account["marketData"]["status"] == "PERMISSION_DENIED_OR_UNAVAILABLE"
    assert account["marketData"]["error"] == FORBIDDEN

    status = web_server.broker_status()
    assert views() == {"CONNECTED"}
    assert status["sessionStatus"] == "ACTIVE"
    assert status["manualTradingBlocked"] is False
    assert status["lastError"] is None
    assert status["capabilities"]["market_data"]["status"] == "UNAVAILABLE"
    assert status["capabilities"]["market_data"]["error"] == FORBIDDEN
    for capability in ("profile", "positions", "orders", "holdings", "margin"):
        assert status["capabilities"][capability]["status"] == "AVAILABLE"


def test_typed_403_on_one_endpoint_keeps_the_client_and_session(service) -> None:
    fail(service, "get_quote", GrowwAPIAuthorisationException(), trading_symbol="X", exchange="NSE", segment="CASH")

    assert service.is_connected() is True
    assert service.current_connection_status() == "CONNECTED"
    assert service.capability_status("market_data")["status"] == "UNAVAILABLE"
    # Other endpoints keep working through the same client.
    assert call(service, "get_positions_for_user") == {"positions": []}


def test_repeated_endpoint_403_records_one_audit_event(monkeypatch, service) -> None:
    events = []
    monkeypatch.setattr(db_module, "record_token_audit_event", lambda **kwargs: events.append(kwargs))
    for _ in range(3):
        fail(service, "get_quote", GrowwAPIAuthorisationException(), trading_symbol="X", exchange="NSE", segment="CASH")

    assert [event["event"] for event in events] == ["CAPABILITY_UNAVAILABLE"]
    assert not any(event["event"] == "CONNECTION_LOST" for event in events)


def test_endpoint_403_does_not_hide_a_broker_level_error(service) -> None:
    fail(service, "get_positions_for_user", GrowwAPITimeoutException())
    fail(service, "get_quote", GrowwAPIAuthorisationException(), trading_symbol="X", exchange="NSE", segment="CASH")

    assert service.current_connection_status() == "ERROR"


# -- 2. expired token -------------------------------------------------------


def test_expired_token_is_still_a_broker_level_disconnect(web_app, service) -> None:
    fail(service, "get_positions_for_user", GrowwAPIAuthenticationException())

    assert views() == {"TOKEN_EXPIRED"}
    assert web_server.broker_status()["manualTradingBlocked"] is True
    assert web_server.broker_status()["capabilities"] == {}
    with pytest.raises(BrokerNotConnectedError):
        service.effective_client()


def test_expired_token_reported_in_a_failure_body_is_still_auth(service) -> None:
    fail(service, "get_order_list", GrowwAPIException(code="GA001", msg="Token expired, please login again"))

    assert service.current_connection_status() == "TOKEN_EXPIRED"
    assert service._client is None


# -- 3. invalid token -------------------------------------------------------


def test_invalid_token_is_rejected_and_never_connected(monkeypatch, fake_groww) -> None:
    service = TokenService(make_settings())

    class RejectingGrowwAPI:
        def __new__(cls, token):
            client = FakeGrowwClient(token)
            client.failures["get_user_profile"] = GrowwAPIAuthenticationException()
            return client

    monkeypatch.setattr(token_service_module, "GrowwAPI", RejectingGrowwAPI)
    with pytest.raises(token_service_module.BrokerValidationError):
        service.update_access_token("bad-token")

    assert service.current_connection_status() == "TOKEN_INVALID"
    assert service.status().token_status == "INVALID"
    with pytest.raises(BrokerNotConnectedError):
        service.effective_client()


def test_a_403_that_says_the_token_is_invalid_is_treated_as_auth(service) -> None:
    fail(service, "get_quote", GrowwAPIException(code="403", msg="Invalid token"),
         trading_symbol="X", exchange="NSE", segment="CASH")

    assert service.current_connection_status() in {"TOKEN_EXPIRED", "TOKEN_INVALID"}
    assert service._client is None


# -- 4. transient API error -------------------------------------------------


def test_transient_error_marks_error_but_keeps_the_client(web_app, service) -> None:
    fail(service, "get_order_list", GrowwAPITimeoutException())

    assert views() == {"ERROR"}
    assert web_server.broker_status()["manualTradingBlocked"] is True
    assert service._client is not None


def test_next_successful_call_restores_connected_after_a_transient_error(web_app, service) -> None:
    fail(service, "get_order_list", GrowwAPITimeoutException())
    service._client.failures.clear()

    call(service, "get_order_list")

    assert views() == {"CONNECTED"}
    assert web_server.broker_status()["lastError"] is None


def test_a_successful_call_never_revives_an_auth_failed_connection(service) -> None:
    client = service._client
    fail(service, "get_order_list", GrowwAPIAuthenticationException())
    service.mark_successful_request(endpoint="get_order_list")  # e.g. a late reply racing the failure

    assert service.current_connection_status() == "TOKEN_EXPIRED"
    assert service._client is None
    assert client is not None


# -- 5. successful recovery after endpoint failure --------------------------


def test_endpoint_recovers_to_available_after_its_403_clears(web_app, service) -> None:
    service._client.failures["get_quote"] = GrowwAPIAuthorisationException()
    web_app.snapshot()
    assert web_server.account()["marketData"]["status"] == "PERMISSION_DENIED_OR_UNAVAILABLE"

    service._client.failures.clear()  # e.g. Live Data permission enabled on the account
    web_app.snapshot()

    assert web_server.account()["marketData"]["status"] == "AVAILABLE"
    assert web_server.broker_status()["capabilities"]["market_data"]["status"] == "AVAILABLE"
    assert views() == {"CONNECTED"}


def test_revalidating_with_a_new_token_resets_capabilities(service) -> None:
    fail(service, "get_quote", GrowwAPIAuthorisationException(), trading_symbol="X", exchange="NSE", segment="CASH")

    service.update_access_token("another-token")

    assert service.capability_status("market_data") is None
    assert service.capability_status("profile")["status"] == "AVAILABLE"
