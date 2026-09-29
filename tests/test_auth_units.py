"""Dashboard sign-in building blocks: identifiers, CAPTCHA generation and
rendering, password hashing/policy, and schema isolation."""

import struct
import zlib
from collections import Counter

import pytest
from argon2 import PasswordHasher
from conftest import CAPTCHA_ANSWER, PASSWORD
from fastapi.testclient import TestClient

from algoedge import auth_captcha, auth_service
from algoedge.auth_models import AuthBase, User
from algoedge.models import Base

# ---------------- identifiers ----------------


@pytest.mark.parametrize("typed, expected", [
    ("  Trader@Example.COM ", ("EMAIL", "trader@example.com")),
    ("9876543210", ("MOBILE", "9876543210")),
    ("+91 98765 43210", ("MOBILE", "9876543210")),
    ("+91-98765-43210", ("MOBILE", "9876543210")),
    ("919876543210", ("MOBILE", "9876543210")),
    ("09876543210", ("MOBILE", "9876543210")),
    ("(987) 654.3210", ("MOBILE", "9876543210")),
    ("5876543210", (None, None)),        # Indian mobiles start 6-9
    ("98765", (None, None)),
    ("not an identifier", (None, None)),
    ("a@b", (None, None)),
    ("x" * 400 + "@example.com", (None, None)),
    (None, (None, None)),
    (12345, (None, None)),
])
def test_identifiers_are_detected_and_normalized_on_the_server(typed, expected) -> None:
    assert auth_service.detect_identifier(typed) == expected


def test_identifiers_are_masked_for_display() -> None:
    assert auth_service.mask_identifier("trader@example.com", "EMAIL") == "t***@example.com"
    assert auth_service.mask_identifier("9876543210", "MOBILE") == "******3210"
    assert auth_service.mask_identifier(None, None) is None


# ---------------- CAPTCHA ----------------


def test_captcha_text_is_six_uppercase_letters_or_digits_and_varies() -> None:
    samples = [auth_captcha.generate_text() for _ in range(2000)]
    assert all(len(text) == 6 and set(text) <= set(auth_captcha.ALPHABET) for text in samples)
    assert len(set(samples)) == len(samples)
    counts = Counter("".join(samples))
    assert set(counts) == set(auth_captcha.ALPHABET)  # every character is reachable


def _png_size_and_pixels(data: bytes) -> tuple[int, int, bytes]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    offset, idat = 8, b""
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        if kind == b"IDAT":
            idat += data[offset + 8:offset + 8 + length]
        offset += 12 + length
    return width, height, zlib.decompress(idat)


def test_the_captcha_png_is_a_valid_image_that_contains_no_text() -> None:
    data = auth_captcha.render_png("A7K2P9")
    width, height, raw = _png_size_and_pixels(data)
    assert (width, height) == (auth_captcha._WIDTH, auth_captcha._HEIGHT)
    assert len(raw) == height * (width + 1)
    assert b"A7K2P9" not in data


def test_the_captcha_store_expires_and_is_single_use(monkeypatch) -> None:
    from conftest import AuthClock

    monkeypatch.setattr(auth_captcha, "generate_text", lambda: CAPTCHA_ANSWER)
    clock = AuthClock()
    store = auth_captcha.CaptchaStore(auth_captcha.timedelta(seconds=120), clock)
    issued = store.issue()
    assert "captchaId" in issued and CAPTCHA_ANSWER not in str(issued)
    assert store.verify_and_consume(issued["captchaId"], "a7k2p9")  # case-insensitive entry
    assert not store.verify_and_consume(issued["captchaId"], CAPTCHA_ANSWER)  # consumed
    late = store.issue()
    clock.advance(seconds=120)
    assert not store.verify_and_consume(late["captchaId"], CAPTCHA_ANSWER)
    assert not store.verify_and_consume("unknown", CAPTCHA_ANSWER)
    assert not store.verify_and_consume(None, None)


def test_the_captcha_store_is_bounded(monkeypatch) -> None:
    from conftest import AuthClock

    monkeypatch.setattr(auth_captcha, "MAX_STORED", 5)
    monkeypatch.setattr(auth_captcha, "render_png", lambda _text: b"")
    store = auth_captcha.CaptchaStore(auth_captcha.timedelta(seconds=120), AuthClock())
    for _ in range(20):
        store.issue()
    assert len(store) == 5


# ---------------- passwords ----------------


def test_passwords_are_hashed_with_argon2id_never_stored_plain() -> None:
    stored = auth_service.hash_password(PASSWORD)
    assert stored.startswith("$argon2id$") and PASSWORD not in stored
    assert auth_service.verify_password(stored, PASSWORD) == (True, False)
    assert auth_service.verify_password(stored, "nope")[0] is False
    assert auth_service.verify_password("not-a-hash", PASSWORD) == (False, False)
    assert auth_service.verify_password(None, PASSWORD) == (False, False)


@pytest.mark.parametrize("password, ok", [
    ("", False), ("short", False), ("exactly8", True), ("a long passphrase with spaces", True),
    ("x" * 256, True), ("x" * 257, False), (None, False),
])
def test_the_password_policy_is_reasonable(password, ok) -> None:
    assert (auth_service.password_policy_error(password, min_length=8) is None) is ok


def test_an_outdated_hash_is_upgraded_on_successful_sign_in(auth_env, monkeypatch) -> None:
    _app, _state, factory, clock = auth_env
    weak = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
    user = auth_service.create_user(factory, email="old@example.com", mobile_no=None, password=PASSWORD,
                                    role="USER", clock=clock, min_length=8)
    with factory() as session:
        before = session.get(User, user["id"]).password_hash
    monkeypatch.setattr(auth_service, "password_hasher", PasswordHasher(time_cost=2, memory_cost=2048, parallelism=1))
    assert weak.check_needs_rehash(before) is False
    client = TestClient(auth_env[0])
    cid = client.get("/api/auth/captcha").json()["captchaId"]
    response = client.post("/api/auth/login", json={"identifier": "old@example.com", "password": PASSWORD,
                                                     "captchaId": cid, "captcha": CAPTCHA_ANSWER})
    assert response.status_code == 200
    with factory() as session:
        after = session.get(User, user["id"]).password_hash
    assert after != before and "t=2" in after and auth_service.verify_password(after, PASSWORD)[0]


# ---------------- schema isolation ----------------


def test_the_auth_tables_are_never_part_of_the_apps_create_all() -> None:
    assert "users" not in Base.metadata.tables and "user_login_activity" not in Base.metadata.tables
    assert set(AuthBase.metadata.tables) == {"users", "user_login_activity"}


def test_the_mapped_columns_match_the_existing_schema() -> None:
    users = {column.name for column in AuthBase.metadata.tables["users"].columns}
    activity = {column.name for column in AuthBase.metadata.tables["user_login_activity"].columns}
    assert users == {"id", "email", "mobile_no", "password_hash", "role", "is_active", "failed_login_count",
                     "locked_until", "last_login_at", "created_at", "updated_at"}
    assert activity == {"id", "user_id", "login_identifier", "identifier_type", "attempt_at", "success",
                        "failure_reason", "ip_address", "user_agent", "session_id"}


def test_on_sql_server_the_types_are_nvarchar_and_datetime2() -> None:
    from sqlalchemy.dialects import mssql
    from sqlalchemy.schema import CreateTable

    ddl = str(CreateTable(AuthBase.metadata.tables["user_login_activity"]).compile(dialect=mssql.dialect()))
    assert "BIGINT" in ddl and "DATETIME2" in ddl and "NVARCHAR(1000)" in ddl


# ---------------- operator CLI ----------------


def test_the_cli_creates_and_resets_users_without_echoing_passwords(auth_env, monkeypatch, capsys) -> None:
    import io

    from algoedge import auth_cli

    factory = auth_env[2]
    monkeypatch.setattr(auth_cli, "_factory", lambda: factory)
    monkeypatch.setattr("sys.stdin", io.StringIO("first admin password\n"))
    assert auth_cli.main(["create-user", "--email", "Boss@Example.com", "--role", "ADMIN", "--password-stdin"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("second admin password\n"))
    assert auth_cli.main(["set-password", "--identifier", "boss@example.com", "--password-stdin"]) == 0
    assert auth_cli.main(["list-users"]) == 0
    out = capsys.readouterr().out
    assert "boss@example.com" in out and "ADMIN" in out
    assert "first admin password" not in out and "second admin password" not in out
    with factory() as session:
        stored = session.query(User).one().password_hash
    assert auth_service.verify_password(stored, "second admin password")[0]
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    with pytest.raises(SystemExit):
        auth_cli.main(["create-user", "--mobile", "9876543210", "--password-stdin"])


def test_verify_schema_is_read_only_and_passes_on_the_expected_schema(auth_env, tmp_path) -> None:
    from sqlalchemy import create_engine, text

    from algoedge import auth_cli

    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    with engine.connect() as connection:
        before = connection.execute(text("SELECT sql FROM sqlite_master ORDER BY name")).all()
    ok, lines = auth_cli.verify_schema(engine)
    assert ok, lines
    assert any("user_login_activity.user_id -> users.id: FOREIGN KEY" in line for line in lines)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT sql FROM sqlite_master ORDER BY name")).all() == before
    engine.dispose()


def test_verify_schema_reports_a_drifted_table(tmp_path) -> None:
    from sqlalchemy import create_engine, text

    from algoedge import auth_cli

    engine = create_engine(f"sqlite:///{tmp_path / 'drift.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(254) NOT NULL)"))
    ok, lines = auth_cli.verify_schema(engine)
    assert not ok
    assert any(line.startswith("FAIL  users: live columns") for line in lines)
    assert any(line.startswith("FAIL  users.email: NULL") for line in lines)
    assert any(line.startswith("FAIL  user_login_activity: table exists") for line in lines)
    engine.dispose()


def test_the_cli_never_runs_init_db_or_any_ddl(auth_env, tmp_path, monkeypatch, capsys) -> None:
    from sqlalchemy import create_engine

    from algoedge import auth_cli, db

    def forbidden(*_args, **_kwargs):
        raise AssertionError("auth_cli must not call db.init_db()")

    monkeypatch.setattr(db, "init_db", forbidden)
    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    monkeypatch.setattr(auth_cli, "_engine", lambda: engine)
    assert auth_cli.main(["verify-schema"]) == 0
    assert auth_cli.main(["list-users"]) == 0
    assert "auth schema: PASS" in capsys.readouterr().out
    engine.dispose()


def test_the_apps_create_all_never_creates_the_auth_tables(tmp_path) -> None:
    from sqlalchemy import create_engine, inspect

    engine = create_engine(f"sqlite:///{tmp_path / 'trading.db'}")
    Base.metadata.create_all(engine)  # what db.init_db() runs
    tables = set(inspect(engine).get_table_names())
    assert "users" not in tables and "user_login_activity" not in tables
    engine.dispose()


def test_sql_server_single_null_unique_rule_is_a_clear_error_not_an_outage(auth_env) -> None:
    # SQL Server lets a plain UNIQUE column hold only one NULL; emulate that on SQLite.
    from sqlalchemy import text

    _app, _state, factory, clock = auth_env
    with factory() as session:
        session.execute(text("CREATE UNIQUE INDEX ux_mobile_like_sql_server ON users (COALESCE(mobile_no, ''))"))
        session.commit()
    auth_service.create_user(factory, email="one@example.com", mobile_no=None, password=PASSWORD, role="USER",
                             clock=clock, min_length=8)
    with pytest.raises(ValueError, match="conflicts with an existing user"):
        auth_service.create_user(factory, email="two@example.com", mobile_no=None, password=PASSWORD,
                                 role="USER", clock=clock, min_length=8)
