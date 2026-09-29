"""Dashboard sign-in: sessions (cookie, timeouts, logout, fixation), CSRF,
server-side authorization, and the policy applied to the real dashboard app."""

import pytest
from conftest import CAPTCHA_ANSWER, PASSWORD
from fastapi.testclient import TestClient

from algoedge import auth_routes
from algoedge.auth_models import User


def sign_in(client, identifier="trader@example.com", password=PASSWORD):
    cid = client.get("/api/auth/captcha").json()["captchaId"]
    response = client.post("/api/auth/login", json={"identifier": identifier, "password": password,
                                                     "captchaId": cid, "captcha": CAPTCHA_ANSWER})
    assert response.status_code == 200, response.text
    return response


def csrf(client):
    return {"X-CSRF-Token": client.get("/api/auth/me").json()["csrfToken"]}


@pytest.fixture()
def client(auth_env):
    return TestClient(auth_env[0])


@pytest.fixture()
def admin(make_user):
    return make_user(email="admin@example.com", mobile="9000000001", role="ADMIN")


# ---------------- sessions ----------------


def test_login_creates_a_server_side_session(client, make_user) -> None:
    user = make_user()
    assert client.get("/api/auth/me").status_code == 401
    sign_in(client)
    me = client.get("/api/auth/me").json()
    assert me["authenticated"] is True and me["user"] == {
        "id": user["id"], "email": "trader@example.com", "mobileNo": "9876543210", "role": "USER"}
    assert "password" not in str(me).lower()


def test_the_session_cookie_is_httponly_lax_scoped_and_expiring(client, make_user) -> None:
    make_user()
    header = sign_in(client).headers["set-cookie"]
    lowered = header.lower()
    assert header.startswith(f"{auth_routes.COOKIE_NAME}=")
    assert "httponly" in lowered and "samesite=lax" in lowered and "path=/" in lowered and "max-age=28800" in lowered
    assert "secure" not in lowered  # plain-HTTP request under the "auto" setting


def test_the_cookie_is_secure_over_https(auth_env, make_user) -> None:
    make_user()
    client = TestClient(auth_env[0], base_url="https://testserver")
    assert "secure" in sign_in(client).headers["set-cookie"].lower()


def test_no_token_or_secret_goes_to_the_url_or_the_body(client, make_user) -> None:
    make_user()
    response = sign_in(client)
    token = response.cookies.get(auth_routes.COOKIE_NAME)
    assert token and token not in response.text


def test_the_session_expires_after_the_idle_timeout(client, make_user, auth_env) -> None:
    make_user()
    sign_in(client)
    auth_env[3].advance(minutes=20)
    assert client.get("/api/private").status_code == 200  # activity keeps it alive
    auth_env[3].advance(minutes=29)
    assert client.get("/api/private").status_code == 200
    auth_env[3].advance(minutes=30)
    assert client.get("/api/private").status_code == 401


def test_the_absolute_timeout_ends_even_an_active_session(client, make_user, auth_env) -> None:
    make_user()
    sign_in(client)
    for _ in range(23):  # 7 h 40 min of steady use
        auth_env[3].advance(minutes=20)
        assert client.get("/api/private").status_code == 200
    auth_env[3].advance(minutes=20)  # 8 h
    assert client.get("/api/private").status_code == 401


def test_logout_invalidates_the_session_and_the_old_cookie_is_useless(client, make_user) -> None:
    make_user()
    sign_in(client)
    old_token = client.cookies.get(auth_routes.COOKIE_NAME)
    response = client.post("/api/auth/logout", headers=csrf(client))
    assert response.status_code == 200 and response.json() == {"authenticated": False}
    assert client.get("/api/private").status_code == 401
    replay = TestClient(client.app)
    replay.cookies.set(auth_routes.COOKIE_NAME, old_token)
    assert replay.get("/api/private").status_code == 401 and replay.get("/api/auth/me").status_code == 401


def test_logout_requires_the_csrf_token(client, make_user) -> None:
    make_user()
    sign_in(client)
    assert client.post("/api/auth/logout").status_code == 403
    assert client.get("/api/private").status_code == 200


def test_session_fixation_a_planted_or_previous_token_is_never_kept(client, make_user) -> None:
    make_user()
    planted = TestClient(client.app, cookies={auth_routes.COOKIE_NAME: "attacker-chosen-token"})
    issued = sign_in(planted).cookies.get(auth_routes.COOKIE_NAME)
    assert issued and issued != "attacker-chosen-token"  # the server always mints its own token
    first = sign_in(client).cookies.get(auth_routes.COOKIE_NAME)
    second = sign_in(client).cookies.get(auth_routes.COOKIE_NAME)  # signing in again replaces the session
    assert second != first
    assert client.get("/api/private").status_code == 200
    stale = TestClient(client.app)
    stale.cookies.set(auth_routes.COOKIE_NAME, first)
    assert stale.get("/api/private").status_code == 401


# ---------------- CSRF ----------------


def test_state_changing_requests_need_the_session_csrf_token(client, make_user, admin) -> None:
    sign_in(client, "admin@example.com")
    assert client.post("/api/private-action").status_code == 403
    assert client.post("/api/private-action", headers={"X-CSRF-Token": "forged"}).status_code == 403
    assert client.post("/api/private-action", headers=csrf(client)).status_code == 200


def test_a_cross_origin_request_is_refused_even_with_the_token(client, admin) -> None:
    sign_in(client, "admin@example.com")
    headers = {**csrf(client), "Origin": "https://evil.example"}
    assert client.post("/api/private-action", headers=headers).status_code == 403


# ---------------- authorization ----------------


def test_unauthenticated_requests_are_refused(client) -> None:
    for path in ("/api/private", "/api/auth/me", "/api/admin/users", "/api/admin/login-activity"):
        assert client.get(path).status_code == 401
    assert client.post("/api/private-action").status_code == 401


def test_a_user_is_read_only_and_cannot_reach_admin_endpoints(client, make_user) -> None:
    make_user()
    sign_in(client)
    assert client.get("/api/private").status_code == 200
    headers = csrf(client)
    assert client.post("/api/private-action", headers=headers).status_code == 403
    assert client.get("/api/admin/users").status_code == 403
    assert client.get("/api/admin/login-activity").status_code == 403
    assert client.post("/api/admin/users/1/role", headers=headers, json={"role": "ADMIN"}).status_code == 403


def test_an_admin_can_reach_admin_endpoints_and_write(client, admin) -> None:
    sign_in(client, "admin@example.com")
    assert client.get("/api/admin/users").status_code == 200
    assert client.get("/api/admin/login-activity").status_code == 200
    assert client.post("/api/private-action", headers=csrf(client)).status_code == 200


def test_the_role_is_rechecked_on_the_server_not_trusted_from_the_session(client, admin, auth_env) -> None:
    sign_in(client, "admin@example.com")
    with auth_env[2]() as session:  # demoted directly in the database, outside the app
        session.get(User, admin["id"]).role = "USER"
        session.commit()
    auth_env[3].advance(seconds=61)
    assert client.get("/api/admin/users").status_code == 403
    assert client.get("/api/auth/me").json()["user"]["role"] == "USER"


def test_deactivating_a_user_ends_their_session(client, make_user, auth_env) -> None:
    user = make_user()
    sign_in(client)
    with auth_env[2]() as session:
        session.get(User, user["id"]).is_active = False
        session.commit()
    auth_env[3].advance(seconds=61)
    assert client.get("/api/private").status_code == 401


def test_the_require_role_dependency_rejects_the_wrong_role() -> None:
    from fastapi import HTTPException

    from algoedge.auth_sessions import Session

    session = Session(user_id=1, role="USER", email=None, mobile_no=None, csrf_token="c", reference="r",
                      created_at=None, last_seen_at=None, validated_at=None)
    with pytest.raises(HTTPException) as refused:
        auth_routes.require_role("ADMIN")(session)
    assert refused.value.status_code == 403
    assert auth_routes.require_role("USER")(session) is session


# ---------------- optional loopback exemption (off by default on this branch) ----------------

# What the collector/preflight/verify.sh send: a direct connection to http://127.0.0.1:5181.
COLLECTOR_BASE = "http://127.0.0.1:5181"
EXEMPT_GETS = ("/api/auto-trading/status", "/api/alerts?limit=200", "/api/auto-trading/option-context/nifty-50")


def loopback_client(app, base_url=COLLECTOR_BASE, peer="127.0.0.1"):
    return TestClient(app, base_url=base_url, client=(peer, 50000))


def test_the_loopback_exemption_is_off_by_default(auth_env) -> None:
    from algoedge.config import Settings

    assert Settings(_env_file=None).auth_loopback_readonly_exempt is False
    assert loopback_client(auth_env[0]).get("/api/auto-trading/status").status_code == 401


@pytest.fixture()
def exempt_app(auth_env):
    auth_env[1].settings.auth_loopback_readonly_exempt = True  # opt-in on this branch
    app = auth_env[0]

    @app.get("/api/alerts")
    def alerts() -> dict:
        return {"alerts": []}

    @app.get("/api/auto-trading/option-context/{index_id}")
    def option_context(index_id: str) -> dict:
        return {"indexId": index_id}

    return app


def test_direct_loopback_may_read_only_the_exempt_endpoints(exempt_app) -> None:
    for base in (COLLECTOR_BASE, "http://localhost:5181", "http://[::1]:5181"):
        peer = "::1" if "::1" in base else "127.0.0.1"
        loopback = loopback_client(exempt_app, base, peer)
        for path in EXEMPT_GETS:
            response = loopback.get(path)  # no cookie, no CAPTCHA, no CSRF token, no role
            assert response.status_code == 200, (base, path)
        assert "set-cookie" not in response.headers
    loopback = loopback_client(exempt_app)
    assert loopback.get("/api/private").status_code == 401
    assert loopback.post("/api/auto-trading/run/nifty-50").status_code == 401  # the drill POST is not exempt
    assert loopback.post("/api/auto-trading/status").status_code == 401  # only GET/HEAD


@pytest.mark.parametrize("header, value", [
    ("X-Forwarded-For", "127.0.0.1"),
    ("X-Real-IP", "127.0.0.1"),
    ("Forwarded", "for=127.0.0.1"),
    ("X-Forwarded-Host", "127.0.0.1:5181"),
    ("X-Forwarded-Proto", "http"),
])
def test_forwarding_headers_claiming_loopback_never_earn_the_exemption(exempt_app, header, value) -> None:
    # Anything relayed by Nginx carries a forwarding header; claiming 127.0.0.1 in it changes nothing.
    loopback = loopback_client(exempt_app)
    for path in EXEMPT_GETS:
        assert loopback.get(path, headers={header: value}).status_code == 401, (header, path)


def test_a_proxy_forwarding_the_public_host_gets_no_exemption(exempt_app) -> None:
    # Even an Nginx that sent no forwarding header at all: `proxy_set_header Host $host` is refused.
    proxied = loopback_client(exempt_app, base_url="https://algoedge.example.com")
    assert proxied.get("/api/auto-trading/status").status_code == 401


def test_a_remote_peer_gets_no_exemption(exempt_app) -> None:
    for peer in ("203.0.113.9", "10.0.0.5", "192.168.1.20"):
        assert loopback_client(exempt_app, peer=peer).get("/api/auto-trading/status").status_code == 401


def test_the_exemption_can_be_switched_off(auth_env) -> None:
    auth_env[1].settings.auth_loopback_readonly_exempt = False
    assert loopback_client(auth_env[0]).get("/api/auto-trading/status").status_code == 401


# ---------------- the real dashboard app ----------------


@pytest.fixture()
def dashboard(auth_env, monkeypatch):
    from algoedge import web_server

    monkeypatch.setattr(web_server.app.state, "auth", auth_env[1])
    return web_server


def test_the_dashboard_requires_sign_in(dashboard, make_user) -> None:
    client = TestClient(dashboard.app)
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login.html"
    assert client.get("/app.js").status_code == 401
    for path in ("/api/positions", "/api/broker/status", "/api/trade-ledger", "/api/system/health"):
        assert client.get(path).status_code == 401, path
    login_page = client.get("/login.html")
    assert login_page.status_code == 200 and "Content-Security-Policy" in login_page.headers


def test_loopback_gets_no_exemption_on_the_dashboard_by_default(dashboard) -> None:
    loopback = TestClient(dashboard.app, base_url="http://127.0.0.1:5181", client=("127.0.0.1", 50000))
    assert loopback.get("/api/auto-trading/status").status_code == 401
    assert loopback.get("/api/alerts?limit=5").status_code == 401
    assert loopback.post("/api/auto-trading/run/nifty-50").status_code == 401


def test_a_user_cannot_engage_the_kill_switch_or_touch_broker_credentials(dashboard, make_user) -> None:
    make_user()
    client = TestClient(dashboard.app)
    sign_in(client)
    headers = csrf(client)
    before = dashboard.risk_manager.state.kill_switch
    for path in ("/api/auto-trading/kill-switch", "/api/auto-trading/enable", "/api/auto-trading/run/nifty-50",
                 "/api/manual-trading/order", "/api/broker/credentials", "/api/broker/access-token",
                 "/api/reconciliation/override", "/api/alerts/acknowledge-all"):
        assert client.post(path, headers=headers, json={}).status_code == 403, path
    assert dashboard.risk_manager.state.kill_switch == before
    assert client.get("/api/auto-trading/status").status_code == 200  # read access is kept
    assert client.get("/admin.html", follow_redirects=False).headers["location"] == "/"


def test_an_admin_can_use_administrative_operations_on_the_dashboard(dashboard, admin) -> None:
    client = TestClient(dashboard.app)
    sign_in(client, "admin@example.com")
    assert client.post("/api/alerts/acknowledge-all", headers=csrf(client)).status_code == 200
    assert client.get("/admin.html").status_code == 200


def test_live_trading_stays_off() -> None:
    from algoedge.config import Settings

    settings = Settings(_env_file=None)
    assert settings.live_trading is False and settings.broker == "paper"


def test_an_exempt_collector_request_does_no_auth_database_or_session_work(exempt_app, auth_env) -> None:
    def no_database():
        raise AssertionError("the exempt path must not touch the database")

    auth_env[1].session_factory = no_database
    sessions_before = len(auth_env[1].sessions)
    loopback = loopback_client(exempt_app)
    for path in EXEMPT_GETS:
        assert loopback.get(path).status_code == 200
    assert len(auth_env[1].sessions) == sessions_before
