"""Dashboard sign-in: ADMIN login-activity view and user management."""

import pytest
from conftest import CAPTCHA_ANSWER, PASSWORD
from fastapi.testclient import TestClient

from algoedge.auth_models import User


def sign_in(client, identifier, password=PASSWORD):
    cid = client.get("/api/auth/captcha").json()["captchaId"]
    return client.post("/api/auth/login", json={"identifier": identifier, "password": password,
                                                 "captchaId": cid, "captcha": CAPTCHA_ANSWER})


def csrf(client):
    return {"X-CSRF-Token": client.get("/api/auth/me").json()["csrfToken"]}


@pytest.fixture()
def admin_client(auth_env, make_user):
    admin = make_user(email="admin@example.com", mobile="9000000001", role="ADMIN")
    client = TestClient(auth_env[0])
    assert sign_in(client, "admin@example.com").status_code == 200
    client.admin = admin
    return client


def test_login_activity_lists_filters_paginates_and_masks(admin_client, auth_env, make_user) -> None:
    user = make_user()
    other = TestClient(auth_env[0])
    sign_in(other, "trader@example.com", "wrong password")
    sign_in(other, "stranger@example.com")
    sign_in(other, "trader@example.com")
    result = admin_client.get("/api/admin/login-activity").json()
    assert result["total"] == 4 and result["page"] == 1
    newest = result["items"][0]
    assert (newest["user"], newest["identifier"], newest["success"]) == ("trader@example.com", "trader@example.com",
                                                                         True)
    unknown = next(item for item in result["items"] if item["userId"] is None)
    assert unknown["identifier"] == "s***@example.com" and unknown["failureReason"] == "INVALID_CREDENTIALS"
    assert set(newest) == {"id", "attemptAt", "userId", "user", "identifier", "identifierType", "success",
                           "failureReason", "ipAddress", "userAgent", "sessionId"}
    failures = admin_client.get("/api/admin/login-activity?success=false").json()
    assert failures["total"] == 2 and all(not item["success"] for item in failures["items"])
    by_reason = admin_client.get("/api/admin/login-activity?failure_reason=INVALID_PASSWORD").json()
    assert [item["userId"] for item in by_reason["items"]] == [user["id"]]
    by_user = admin_client.get(f"/api/admin/login-activity?user_id={user['id']}").json()
    assert by_user["total"] == 2
    page = admin_client.get("/api/admin/login-activity?page=2&page_size=3").json()
    assert page["total"] == 4 and len(page["items"]) == 1
    today = auth_env[3]().date().isoformat()
    assert admin_client.get(f"/api/admin/login-activity?start={today}&end={today}").json()["total"] == 4
    assert admin_client.get("/api/admin/login-activity?start=2026-10-02").json()["total"] == 0
    assert admin_client.get("/api/admin/login-activity?start=yesterday").status_code == 400
    assert admin_client.get("/api/admin/login-activity?failure_reason=DROP TABLE").status_code == 400


def test_login_activity_never_exposes_secrets(admin_client, auth_env) -> None:
    with auth_env[2]() as session:
        stored = session.get(User, admin_client.admin["id"]).password_hash
    text = admin_client.get("/api/admin/login-activity").text + admin_client.get("/api/admin/users").text
    for secret in (PASSWORD, CAPTCHA_ANSWER, stored, "password_hash", "passwordHash"):
        assert secret not in text


def test_users_are_listed_without_hashes(admin_client, make_user) -> None:
    make_user()
    users = admin_client.get("/api/admin/users").json()
    assert {user["email"] for user in users["users"]} == {"admin@example.com", "trader@example.com"}
    assert users["roles"] == ["ADMIN", "USER"]


def test_an_admin_creates_a_user_with_a_hashed_password(admin_client, auth_env) -> None:
    response = admin_client.post("/api/admin/users", headers=csrf(admin_client),
                                 json={"mobileNo": "+91 91234 56789", "password": "a fine password", "role": "USER"})
    assert response.status_code == 201
    created = response.json()["user"]
    assert created["mobileNo"] == "9123456789" and "a fine password" not in response.text
    with auth_env[2]() as session:
        assert session.get(User, created["id"]).password_hash.startswith("$argon2id$")
    duplicate = admin_client.post("/api/admin/users", headers=csrf(admin_client),
                                  json={"mobileNo": "09123456789", "password": "a fine password"})
    assert duplicate.status_code == 400
    weak = admin_client.post("/api/admin/users", headers=csrf(admin_client),
                             json={"email": "new@example.com", "password": "short"})
    assert weak.status_code == 400 and "short" not in weak.text


def test_deactivating_a_user_blocks_sign_in_and_ends_sessions(admin_client, auth_env, make_user) -> None:
    user = make_user()
    victim = TestClient(auth_env[0])
    assert sign_in(victim, "trader@example.com").status_code == 200
    response = admin_client.post(f"/api/admin/users/{user['id']}/active", headers=csrf(admin_client),
                                 json={"active": False})
    assert response.status_code == 200 and response.json()["user"]["isActive"] is False
    assert victim.get("/api/auth/me").status_code == 401
    assert sign_in(victim, "trader@example.com").status_code == 401
    admin_client.post(f"/api/admin/users/{user['id']}/active", headers=csrf(admin_client), json={"active": True})
    assert sign_in(victim, "trader@example.com").status_code == 200


def test_role_changes_apply_immediately(admin_client, auth_env, make_user) -> None:
    user = make_user()
    promoted = TestClient(auth_env[0])
    sign_in(promoted, "trader@example.com")
    assert promoted.get("/api/admin/users").status_code == 403
    response = admin_client.post(f"/api/admin/users/{user['id']}/role", headers=csrf(admin_client),
                                 json={"role": "ADMIN"})
    assert response.json()["user"]["role"] == "ADMIN"
    assert promoted.get("/api/admin/users").status_code == 401  # re-sign-in picks up the new role
    sign_in(promoted, "trader@example.com")
    assert promoted.get("/api/admin/users").status_code == 200
    assert admin_client.post(f"/api/admin/users/{user['id']}/role", headers=csrf(admin_client),
                             json={"role": "ROOT"}).status_code == 400


def test_an_admin_cannot_lock_themselves_or_the_system_out(admin_client) -> None:
    admin_id = admin_client.admin["id"]
    headers = csrf(admin_client)
    assert admin_client.post(f"/api/admin/users/{admin_id}/active", headers=headers,
                             json={"active": False}).status_code == 400
    assert admin_client.post(f"/api/admin/users/{admin_id}/role", headers=headers,
                             json={"role": "USER"}).status_code == 400


def test_a_password_reset_replaces_the_hash_clears_the_lock_and_signs_the_user_out(admin_client, auth_env,
                                                                                   make_user) -> None:
    user = make_user()
    victim = TestClient(auth_env[0])
    for _ in range(5):
        sign_in(victim, "trader@example.com", "wrong password")
    response = admin_client.post(f"/api/admin/users/{user['id']}/password", headers=csrf(admin_client),
                                 json={"password": "brand new password"})
    assert response.status_code == 200 and "brand new password" not in response.text
    assert response.json()["user"]["lockedUntil"] is None
    assert sign_in(victim, "trader@example.com").status_code == 401
    assert sign_in(victim, "trader@example.com", "brand new password").status_code == 200


def test_unknown_users_are_404(admin_client) -> None:
    assert admin_client.post("/api/admin/users/999/active", headers=csrf(admin_client),
                             json={"active": True}).status_code == 404
