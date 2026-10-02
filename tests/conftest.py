import pytest

from fno_signals import broker as fno_broker_module


@pytest.fixture(autouse=True)
def _clear_fno_instruments_cache():
    """fno_signals.broker._get_instruments() caches Groww's instrument
    master for 5 minutes (real data doesn't change intraday - see its own
    docstring). Several test files build tiny fake instrument sets through
    it (test_fno_signals_broker.py, test_live_grid.py's
    check_instrument_master() tests, test_manual_trading.py's
    resolve_manual_contract() - which delegates to this same cache, not its
    own module-level one). Without a session-wide reset, whichever test
    happens to run first leaves its fake data behind for every later test
    in the run - this has been the actual root cause of three separate
    cross-file test failures this session already. One autouse fixture
    here replaces the same clear-before/clear-after boilerplate that had
    been copy-pasted into each file individually.
    """
    fno_broker_module._instruments_cache.clear()
    yield
    fno_broker_module._instruments_cache.clear()


# ---------------------------------------------------------------- authentication fixtures
# Shared by tests/test_auth_*.py. A throwaway SQLite copy of the existing
# users / user_login_activity schema (created here, for tests only - the
# application never creates these tables), a controllable clock, a known
# CAPTCHA answer and cheap Argon2id parameters so the suite stays fast.

CAPTCHA_ANSWER = "A7K2P9"
PASSWORD = "correct horse battery"


class AuthClock:
    def __init__(self):
        from datetime import datetime

        from algoedge.risk_manager import IST

        self.now = datetime(2026, 10, 1, 10, 0, tzinfo=IST)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        from datetime import timedelta

        self.now += timedelta(**delta)


@pytest.fixture()
def auth_env(tmp_path, monkeypatch):
    """(app, state, factory, clock) - a minimal FastAPI app with auth installed
    plus a protected read route, a protected write route and a loopback-exempt
    status route, backed by a SQLite users/login-activity schema."""
    from argon2 import PasswordHasher
    from fastapi import FastAPI
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from algoedge import auth_captcha, auth_routes, auth_service
    from algoedge.auth_models import AuthBase
    from algoedge.config import Settings

    monkeypatch.setattr(auth_service, "password_hasher", PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1))
    monkeypatch.setattr(auth_service, "_dummy_hash", None)
    monkeypatch.setattr(auth_captcha, "generate_text", lambda: CAPTCHA_ANSWER)
    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}", connect_args={"check_same_thread": False})
    AuthBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    clock = AuthClock()
    settings = Settings(_env_file=None, db_server="")
    state = auth_routes.build_state(settings, clock=clock, session_factory=lambda: factory)
    app = FastAPI()
    auth_routes.install(app, state)

    @app.get("/api/private")
    def private() -> dict:
        return {"ok": True}

    @app.post("/api/private-action")
    def private_action() -> dict:
        return {"done": True}

    @app.get("/api/auto-trading/status")
    def status() -> dict:
        return {"enabled": False}

    @app.post("/api/auto-trading/run/{index_id}")
    def run(index_id: str) -> dict:
        return {"ran": index_id}

    yield app, state, factory, clock
    engine.dispose()


@pytest.fixture()
def make_user(auth_env):
    from algoedge import auth_service

    _app, _state, factory, clock = auth_env

    def make(email="trader@example.com", mobile="9876543210", role="USER", password=PASSWORD, active=True):
        user = auth_service.create_user(factory, email=email, mobile_no=mobile, password=password, role=role,
                                        clock=clock, min_length=8)
        if not active:
            from algoedge.auth_models import User

            with factory() as session:
                session.get(User, user["id"]).is_active = False
                session.commit()
        return user

    return make


@pytest.fixture(autouse=True)
def _isolate_shared_market_data_service():
    """groww_market_data keeps one process-wide TokenService. Tests that build
    a real session (generate_daily_session) or fetch candles register one, so
    put back whatever was there before each test to stop it leaking."""
    from algoedge import groww_market_data

    previous = groww_market_data._shared_service
    yield
    groww_market_data.use_token_service(previous)
