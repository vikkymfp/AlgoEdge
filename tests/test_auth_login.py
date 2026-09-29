"""Dashboard sign-in: login outcomes, CAPTCHA, lockout and the
user_login_activity record of every attempt."""

import logging

import pytest
from conftest import CAPTCHA_ANSWER, PASSWORD
from fastapi.testclient import TestClient
from sqlalchemy import select

from algoedge import auth_captcha
from algoedge.auth_models import User, UserLoginActivity

GENERIC = "Invalid login credentials."


def captcha_id(client):
    response = client.get("/api/auth/captcha")
    assert response.status_code == 200
    return response.json()["captchaId"]


def login(client, identifier, password=PASSWORD, *, captcha=CAPTCHA_ANSWER, cid=None):
    cid = cid or captcha_id(client)
    return client.post("/api/auth/login",
                       json={"identifier": identifier, "password": password, "captchaId": cid, "captcha": captcha})


def activity(factory):
    with factory() as session:
        return list(session.scalars(select(UserLoginActivity).order_by(UserLoginActivity.id)))


def user_row(factory, user_id):
    with factory() as session:
        return session.get(User, user_id)


@pytest.fixture()
def client(auth_env):
    app, *_ = auth_env
    return TestClient(app)


# ---------------- login ----------------


def test_valid_email_password_and_captcha_signs_in(client, make_user, auth_env) -> None:
    user = make_user()
    response = login(client, "  Trader@Example.COM ")
    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True and body["user"] == {
        "id": user["id"], "email": "trader@example.com", "mobileNo": "9876543210", "role": "USER"}
    assert body["csrfToken"]
    assert client.get("/api/private").json() == {"ok": True}


@pytest.mark.parametrize("typed", ["9876543210", "+91 98765 43210", "098765-43210", "(91) 9876543210"])
def test_valid_mobile_in_any_common_format_signs_in(client, make_user, typed) -> None:
    make_user()
    assert login(client, typed).status_code == 200


def test_wrong_password_fails_generically(client, make_user) -> None:
    make_user()
    response = login(client, "trader@example.com", "wrong password")
    assert response.status_code == 401 and response.json()["detail"] == GENERIC


@pytest.mark.parametrize("identifier", ["nobody@example.com", "9123456789"])
def test_unknown_account_fails_with_the_same_generic_message(client, make_user, identifier) -> None:
    make_user()
    response = login(client, identifier)
    assert response.status_code == 401 and response.json()["detail"] == GENERIC


def test_disabled_account_fails_generically(client, make_user) -> None:
    make_user(active=False)
    response = login(client, "trader@example.com")
    assert response.status_code == 401 and response.json()["detail"] == GENERIC


def test_locked_account_fails_generically_even_with_the_right_password(client, make_user, auth_env) -> None:
    user = make_user()
    for _ in range(5):
        login(client, "trader@example.com", "wrong password")
    response = login(client, "trader@example.com")
    assert response.status_code == 401 and response.json()["detail"] == GENERIC
    assert activity(auth_env[2])[-1].failure_reason == "ACCOUNT_LOCKED"
    assert user_row(auth_env[2], user["id"]).locked_until is not None


def test_invalid_captcha_fails(client, make_user) -> None:
    make_user()
    response = login(client, "trader@example.com", captcha="ZZZZZZ")
    assert response.status_code == 400 and response.json()["code"] == "INVALID_CAPTCHA"
    assert client.get("/api/private").status_code == 401


def test_expired_captcha_fails(client, make_user, auth_env) -> None:
    make_user()
    cid = captcha_id(client)
    auth_env[3].advance(seconds=121)
    response = login(client, "trader@example.com", cid=cid)
    assert response.status_code == 400 and response.json()["code"] == "INVALID_CAPTCHA"


def test_a_captcha_cannot_be_reused_after_success_or_failure(client, make_user) -> None:
    make_user()
    cid = captcha_id(client)
    assert login(client, "trader@example.com", cid=cid).status_code == 200
    assert login(client, "trader@example.com", cid=cid).json()["code"] == "INVALID_CAPTCHA"
    wrong = captcha_id(client)
    assert login(client, "trader@example.com", captcha="XXXXXX", cid=wrong).status_code == 400
    assert login(client, "trader@example.com", cid=wrong).json()["code"] == "INVALID_CAPTCHA"  # one guess only


def test_each_request_regenerates_a_new_captcha_without_revealing_it(client, auth_env) -> None:
    first, second = client.get("/api/auth/captcha").json(), client.get("/api/auth/captcha").json()
    assert first["captchaId"] != second["captchaId"]
    assert set(first) == {"captchaId", "image", "expiresInSeconds"}
    assert first["image"].startswith("data:image/png;base64,")
    assert CAPTCHA_ANSWER not in str(first) and CAPTCHA_ANSWER.lower() not in str(first).lower()
    assert first["expiresInSeconds"] == 120


def test_captcha_issuance_is_rate_limited(client, auth_env) -> None:
    statuses = [client.get("/api/auth/captcha").status_code for _ in range(61)]
    assert statuses[:60] == [200] * 60 and statuses[60] == 429


def test_the_password_hash_is_never_returned(client, make_user, auth_env) -> None:
    user = make_user()
    stored = user_row(auth_env[2], user["id"]).password_hash
    responses = [login(client, "trader@example.com"), client.get("/api/auth/me")]
    for response in responses:
        assert stored not in response.text and "password" not in response.text.lower()


def test_neither_password_nor_captcha_nor_hash_is_logged(client, make_user, auth_env, caplog) -> None:
    user = make_user()
    stored = user_row(auth_env[2], user["id"]).password_hash
    caplog.set_level(logging.DEBUG)
    login(client, "trader@example.com", "wrong password")
    login(client, "trader@example.com", captcha="QQQQQQ")
    login(client, "trader@example.com")
    assert "auth_event event=LOGIN_SUCCESS" in caplog.text and "event=LOGIN_FAILURE" in caplog.text
    for secret in (PASSWORD, "wrong password", CAPTCHA_ANSWER, stored):
        assert secret not in caplog.text


def test_a_malformed_body_is_rejected_without_echoing_it(client) -> None:
    response = client.post("/api/auth/login", json=["secret-value"])
    assert response.status_code == 400 and "secret-value" not in response.text
    response = client.post("/api/auth/login", json={"identifier": "a@b.co", "password": 123, "captchaId": "x",
                                                     "captcha": "y"})
    assert response.status_code == 400 and "123" not in response.text


def test_login_is_rate_limited_per_client_ip(client, make_user, auth_env) -> None:
    make_user()
    for _ in range(20):
        login(client, "trader@example.com", "wrong password")
    response = login(client, "trader@example.com")
    assert response.status_code == 429 and response.json()["code"] == "RATE_LIMITED"
    assert activity(auth_env[2])[-1].failure_reason == "RATE_LIMITED"


def test_a_cross_origin_login_post_is_refused(client, make_user) -> None:
    make_user()
    cid = captcha_id(client)
    response = client.post("/api/auth/login", headers={"Origin": "https://evil.example"},
                           json={"identifier": "trader@example.com", "password": PASSWORD, "captchaId": cid,
                                 "captcha": CAPTCHA_ANSWER})
    assert response.status_code == 403


def test_no_database_means_no_sign_in(auth_env, make_user) -> None:
    app, state, _factory, _clock = auth_env
    state.session_factory = lambda: None
    client = TestClient(app)
    response = login(client, "trader@example.com")
    assert response.status_code == 503 and client.get("/api/private").status_code == 401


# ---------------- lockout ----------------


def test_failed_passwords_increment_the_counter(client, make_user, auth_env) -> None:
    user = make_user()
    for expected in (1, 2, 3):
        login(client, "trader@example.com", "wrong password")
        assert user_row(auth_env[2], user["id"]).failed_login_count == expected


def test_the_account_locks_at_the_configured_threshold_and_unlocks_after(client, make_user, auth_env) -> None:
    user = make_user()
    for _ in range(4):
        login(client, "trader@example.com", "wrong password")
    assert user_row(auth_env[2], user["id"]).locked_until is None
    login(client, "trader@example.com", "wrong password")
    row = user_row(auth_env[2], user["id"])
    assert row.locked_until is not None and row.failed_login_count == 0
    assert login(client, "trader@example.com").status_code == 401  # locked: right password refused
    auth_env[3].advance(minutes=15, seconds=1)
    assert login(client, "trader@example.com").status_code == 200


def test_attempts_while_locked_do_not_extend_the_lock(client, make_user, auth_env) -> None:
    user = make_user()
    for _ in range(5):
        login(client, "trader@example.com", "wrong password")
    locked_until = user_row(auth_env[2], user["id"]).locked_until
    auth_env[3].advance(minutes=5)
    for _ in range(3):
        login(client, "trader@example.com", "wrong password")
    assert user_row(auth_env[2], user["id"]).locked_until == locked_until


def test_captcha_failures_do_not_count_toward_the_lock(client, make_user, auth_env) -> None:
    user = make_user()
    for _ in range(8):
        login(client, "trader@example.com", "wrong password", captcha="BBBBBB")
    row = user_row(auth_env[2], user["id"])
    assert row.failed_login_count == 0 and row.locked_until is None


def test_success_resets_the_counter_clears_the_lock_and_records_last_login(client, make_user, auth_env) -> None:
    user = make_user()
    for _ in range(3):
        login(client, "trader@example.com", "wrong password")
    with auth_env[2]() as session:  # an expired lock left behind
        row = session.get(User, user["id"])
        row.locked_until = auth_env[3]().replace(tzinfo=None).replace(year=2026, month=9)
        session.commit()
    assert login(client, "trader@example.com").status_code == 200
    row = user_row(auth_env[2], user["id"])
    assert (row.failed_login_count, row.locked_until) == (0, None)
    assert row.last_login_at == auth_env[3]().replace(tzinfo=None)


# ---------------- login activity ----------------


def test_every_kind_of_attempt_is_recorded(client, make_user, auth_env) -> None:
    active = make_user()
    disabled = make_user(email="gone@example.com", mobile="9123400000", active=False)
    login(client, "trader@example.com")                               # success
    login(client, "trader@example.com", "wrong password")             # wrong password
    login(client, "trader@example.com", captcha="CCCCCC")             # invalid CAPTCHA
    login(client, "nobody@example.com")                               # unknown account
    login(client, "gone@example.com")                                 # disabled
    for _ in range(4):
        login(client, "9876543210", "wrong password")                 # reaches the lock (5th failure)
    login(client, "9876543210")                                       # locked
    rows = activity(auth_env[2])
    summary = [(r.user_id, r.login_identifier, r.identifier_type, r.success, r.failure_reason) for r in rows]
    assert summary[:5] == [
        (active["id"], "trader@example.com", "EMAIL", True, None),
        (active["id"], "trader@example.com", "EMAIL", False, "INVALID_PASSWORD"),
        (active["id"], "trader@example.com", "EMAIL", False, "INVALID_CAPTCHA"),  # user known, no counter
        (None, "nobody@example.com", "EMAIL", False, "INVALID_CREDENTIALS"),
        (disabled["id"], "gone@example.com", "EMAIL", False, "ACCOUNT_DISABLED"),
    ]
    assert summary[-1] == (active["id"], "9876543210", "MOBILE", False, "ACCOUNT_LOCKED")
    assert all(r.attempt_at is not None and r.ip_address == "testclient" and r.user_agent for r in rows)
    assert rows[0].session_id and len(rows[0].session_id) == 32 and all(r.session_id is None for r in rows[1:])


def test_activity_rows_hold_no_password_captcha_or_hash(client, make_user, auth_env) -> None:
    user = make_user()
    stored = user_row(auth_env[2], user["id"]).password_hash
    login(client, "trader@example.com", "wrong password")
    login(client, "trader@example.com", captcha="DDDDDD")
    login(client, "trader@example.com")
    for row in activity(auth_env[2]):
        values = " ".join(str(getattr(row, column.name)) for column in UserLoginActivity.__table__.columns)
        for secret in (PASSWORD, "wrong password", CAPTCHA_ANSWER, "DDDDDD", stored):
            assert secret not in values


def test_a_non_identifier_is_not_stored_verbatim(client, make_user, auth_env) -> None:
    make_user()
    login(client, "my secret passphrase typed in the wrong box")
    row = activity(auth_env[2])[-1]
    assert (row.login_identifier, row.identifier_type, row.failure_reason) == (None, None, "INVALID_CREDENTIALS")


def test_the_captcha_store_never_holds_plaintext(auth_env) -> None:
    store = auth_env[1].captchas
    store.issue()
    assert CAPTCHA_ANSWER not in repr(vars(store)) and len(store) == 1
    assert auth_captcha.LENGTH == 6
