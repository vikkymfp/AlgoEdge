"""Groww credential resolution must be identical for the web dashboard
(TokenService, constructed directly in web_server.py) and `fno_signals
--live` (fno_signals.broker.generate_daily_session). Both paths go through
TokenService: ALGOEDGE_GROWW_* settings are the initial/fallback values and
encrypted credentials saved from API Management override them."""

import logging

import pytest
from cryptography.fernet import Fernet
from growwapi.groww.exceptions import GrowwAPIAuthenticationException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from algoedge import credential_manager
from algoedge import db as db_module
from algoedge import token_service as token_service_module
from algoedge.config import Settings
from algoedge.models import Base
from algoedge.token_service import TokenService
from fno_signals import broker as fno_broker_module
from fno_signals.broker import GrowwSessionError, generate_daily_session

ENCRYPTION_KEY = Fernet.generate_key().decode()
ENV_SECRET = "env-secret-value-should-never-leak"
DB_SECRET = "db-secret-value-should-never-leak"


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
    return db_module._session_factory


def make_settings(**overrides) -> Settings:
    """Explicit overrides for every credential/db field so tests never read
    this machine's real .env Groww credentials or touch its SQL Server."""
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
    def __init__(self, token: str, profile_error: Exception | None = None) -> None:
        self.token = token
        self._profile_error = profile_error

    def get_user_profile(self) -> dict:
        if self._profile_error is not None:
            raise self._profile_error
        return {"user_id": "u1"}


def install_fake_groww(monkeypatch, *, profile_error=None, rejected_tokens=()):
    """Patches the one GrowwAPI TokenService uses. get_access_token mints a
    token derived from the key/secret so tests can tell which pair was used."""

    class FakeGrowwAPI:
        def __new__(cls, token):
            error = profile_error or (GrowwAPIAuthenticationException() if token in rejected_tokens else None)
            return FakeGrowwClient(token, profile_error=error)

        @staticmethod
        def get_access_token(api_key, totp=None, secret=None):
            return f"minted-from-{api_key}"

    monkeypatch.setattr(token_service_module, "GrowwAPI", FakeGrowwAPI)


def store_db_credentials(*, api_key=None, api_secret=None, access_token=None) -> None:
    fields = {}
    if api_key:
        fields["encrypted_api_key"] = credential_manager.encrypt(api_key, ENCRYPTION_KEY)
    if api_secret:
        fields["encrypted_api_secret"] = credential_manager.encrypt(api_secret, ENCRYPTION_KEY)
    if access_token:
        fields["encrypted_access_token"] = credential_manager.encrypt(access_token, ENCRYPTION_KEY)
    assert db_module.save_broker_credential("groww", **fields) is True


def web_client(settings: Settings):
    """The web dashboard's path: web_server.py builds TokenService(settings)
    and every broker call goes through effective_client()."""
    return TokenService(settings).effective_client()


def fno_live_client(settings: Settings, monkeypatch):
    """The `fno_signals --live` path: main() calls generate_daily_session()
    with no arguments, so settings come from get_settings()."""
    monkeypatch.setattr(fno_broker_module, "get_settings", lambda: settings)
    return generate_daily_session()


PATHS = {
    "web": lambda settings, _monkeypatch: web_client(settings),
    "fno_signals_live": fno_live_client,
}


@pytest.fixture(params=list(PATHS))
def resolve_client(request, monkeypatch):
    path = PATHS[request.param]
    return lambda settings: path(settings, monkeypatch)


# -- environment as initial/fallback configuration ----------------------


def test_env_access_token_is_used_when_nothing_is_stored(monkeypatch, resolve_client) -> None:
    install_fake_groww(monkeypatch)

    client = resolve_client(make_settings(groww_access_token="env-token"))

    assert client.token == "env-token"


def test_env_api_key_and_secret_are_used_when_no_token(monkeypatch, resolve_client) -> None:
    install_fake_groww(monkeypatch)

    client = resolve_client(make_settings(groww_api_key="env-key", groww_api_secret=ENV_SECRET))

    assert client.token == "minted-from-env-key"


def test_env_credentials_are_used_when_db_has_no_row(monkeypatch, resolve_client, sqlite_db) -> None:
    install_fake_groww(monkeypatch)

    client = resolve_client(make_settings(
        groww_access_token="env-token", credential_encryption_key=ENCRYPTION_KEY,
    ))

    assert client.token == "env-token"


# -- stored (API Management) credentials override the environment -------


def test_db_access_token_overrides_env_access_token(monkeypatch, resolve_client, sqlite_db) -> None:
    install_fake_groww(monkeypatch)
    store_db_credentials(access_token="db-token")

    client = resolve_client(make_settings(
        groww_access_token="env-token", credential_encryption_key=ENCRYPTION_KEY,
    ))

    assert client.token == "db-token"


def test_db_api_key_and_secret_override_env_pair(monkeypatch, resolve_client, sqlite_db) -> None:
    install_fake_groww(monkeypatch)
    store_db_credentials(api_key="db-key", api_secret=DB_SECRET)

    client = resolve_client(make_settings(
        groww_api_key="env-key", groww_api_secret=ENV_SECRET, credential_encryption_key=ENCRYPTION_KEY,
    ))

    assert client.token == "minted-from-db-key"


def test_expired_db_token_falls_back_to_db_key_not_env_token(monkeypatch, resolve_client, sqlite_db) -> None:
    install_fake_groww(monkeypatch, rejected_tokens={"db-token"})
    store_db_credentials(api_key="db-key", api_secret=DB_SECRET, access_token="db-token")

    client = resolve_client(make_settings(
        groww_access_token="env-token", groww_api_key="env-key", groww_api_secret=ENV_SECRET,
        credential_encryption_key=ENCRYPTION_KEY,
    ))

    assert client.token == "minted-from-db-key"


def test_credentials_saved_from_api_management_reach_fno_signals_live(monkeypatch, sqlite_db) -> None:
    """End to end: an update through the web app's TokenService (the Update
    API key & secret button) is what the next `fno_signals --live` run uses,
    even though the environment still holds the old values."""
    install_fake_groww(monkeypatch)
    settings = make_settings(
        groww_api_key="env-key", groww_api_secret=ENV_SECRET, credential_encryption_key=ENCRYPTION_KEY,
    )
    TokenService(settings).update_credentials("ui-key", DB_SECRET)

    client = fno_live_client(settings, monkeypatch)

    assert client.token == "minted-from-ui-key"


def test_undecryptable_db_credentials_fall_back_to_env(monkeypatch, resolve_client, sqlite_db) -> None:
    install_fake_groww(monkeypatch)
    store_db_credentials(access_token="db-token")

    client = resolve_client(make_settings(
        groww_access_token="env-token", credential_encryption_key=Fernet.generate_key().decode(),
    ))

    assert client.token == "env-token"


# -- fno_signals --live failure behavior ----------------------------------


def test_fno_live_fails_without_any_credentials(monkeypatch) -> None:
    install_fake_groww(monkeypatch)

    with pytest.raises(GrowwSessionError):
        fno_live_client(make_settings(), monkeypatch)


def test_fno_live_fails_when_verification_fails(monkeypatch) -> None:
    install_fake_groww(monkeypatch, profile_error=GrowwAPIAuthenticationException())

    with pytest.raises(GrowwSessionError):
        fno_live_client(make_settings(groww_access_token="bad-token"), monkeypatch)


def test_fno_live_failure_never_exposes_credentials(monkeypatch, sqlite_db, caplog) -> None:
    install_fake_groww(monkeypatch, profile_error=GrowwAPIAuthenticationException())
    store_db_credentials(api_key="db-key", api_secret=DB_SECRET, access_token="db-token")
    caplog.set_level(logging.DEBUG)

    with pytest.raises(GrowwSessionError) as raised:
        fno_live_client(make_settings(
            groww_api_key="env-key", groww_api_secret=ENV_SECRET, credential_encryption_key=ENCRYPTION_KEY,
        ), monkeypatch)

    for secret in (ENV_SECRET, DB_SECRET, "db-token"):
        assert secret not in str(raised.value)
        assert secret not in caplog.text
    for event in db_module.list_token_audit_events(broker="groww"):
        for secret in (ENV_SECRET, DB_SECRET, "db-token"):
            assert secret not in str(event)
