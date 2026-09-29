"""Operator commands for dashboard users (the first ADMIN has to come from somewhere).

    PYTHONPATH=src python -m algoedge.auth_cli verify-schema          # read-only
    PYTHONPATH=src python -m algoedge.auth_cli list-users             # read-only
    PYTHONPATH=src python -m algoedge.auth_cli create-user --email you@example.com --role ADMIN
    PYTHONPATH=src python -m algoedge.auth_cli create-user --mobile 9876543210
    PYTHONPATH=src python -m algoedge.auth_cli set-password --identifier you@example.com

The password is read with a hidden prompt (entered twice), or from one line of
stdin with --password-stdin - never from the command line, and never printed.

Uses the application's own ALGOEDGE_DB_* settings and connection URL builder
(algoedge.db._odbc_connection_url), but NOT db.init_db(): no command here
creates a database or table, runs create_all(), or applies the trading-table
column migrations. The users / user_login_activity tables must already exist;
verify-schema and list-users only ever SELECT.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime
from typing import Any

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from algoedge import auth_service, db
from algoedge.auth_models import AuthBase, User
from algoedge.config import get_settings
from algoedge.risk_manager import IST

# The existing schema (not created by this app): column -> (SQL Server type, length, nullable).
EXPECTED_SCHEMA: dict[str, dict[str, tuple[str, int | None, bool]]] = {
    "users": {
        "id": ("INT", None, False),
        "email": ("NVARCHAR", 254, True),
        "mobile_no": ("NVARCHAR", 20, True),
        "password_hash": ("NVARCHAR", 500, False),
        "role": ("NVARCHAR", 50, False),
        "is_active": ("BIT", None, False),
        "failed_login_count": ("INT", None, False),
        "locked_until": ("DATETIME2", None, True),
        "last_login_at": ("DATETIME2", None, True),
        "created_at": ("DATETIME2", None, False),
        "updated_at": ("DATETIME2", None, True),
    },
    "user_login_activity": {
        "id": ("BIGINT", None, False),
        "user_id": ("INT", None, True),
        "login_identifier": ("NVARCHAR", 254, True),
        "identifier_type": ("NVARCHAR", 20, True),
        "attempt_at": ("DATETIME2", None, False),
        "success": ("BIT", None, False),
        "failure_reason": ("NVARCHAR", 50, True),
        "ip_address": ("NVARCHAR", 45, True),
        "user_agent": ("NVARCHAR", 1000, True),
        "session_id": ("NVARCHAR", 100, True),
    },
}
_TYPE_ALIASES = {"INTEGER": "INT"}


def _clock() -> datetime:
    return datetime.now(IST)


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("New password: ")
    if getpass.getpass("Repeat password: ") != first:
        raise SystemExit("Passwords do not match.")
    return first


def _engine() -> Engine:
    """The app's database, connected WITHOUT db.init_db() - no DDL of any kind."""
    settings = get_settings()
    if not settings.db_server:
        raise SystemExit("ALGOEDGE_DB_SERVER is not set - no database configured.")
    try:
        return create_engine(db._odbc_connection_url(settings, settings.db_name), pool_pre_ping=True)
    except ValueError as error:  # e.g. SQL authentication selected without credentials
        raise SystemExit(str(error)) from error


def _factory() -> sessionmaker:
    return sessionmaker(bind=_engine())


# ---------------------------------------------------------------- verify-schema (read-only)


def _type_name(column_type: Any) -> str:
    name = type(column_type).__name__.upper()
    return _TYPE_ALIASES.get(name, name)


def _mssql_unique_indexes(connection, table: str) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT i.name, i.is_primary_key, i.is_unique_constraint, i.has_filter, i.filter_definition, c.name "
        "FROM sys.indexes i "
        "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
        "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
        "WHERE i.object_id = OBJECT_ID(:table) AND i.is_unique = 1 AND ic.is_included_column = 0"), {"table": table})
    indexes: dict[str, dict[str, Any]] = {}
    for name, is_pk, is_constraint, has_filter, filter_definition, column in rows:
        entry = indexes.setdefault(name, {"name": name, "primary": bool(is_pk), "constraint": bool(is_constraint),
                                          "filter": filter_definition if has_filter else None, "columns": []})
        entry["columns"].append(column)
    return list(indexes.values())


def verify_schema(engine: Engine) -> tuple[bool, list[str]]:
    """Compares the live users / user_login_activity tables with the ORM
    mapping. SELECT/catalog reads only. Returns (ok, report lines)."""
    lines: list[str] = []
    ok = True

    def check(passed: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        lines.append(f"{'PASS' if passed else 'FAIL'}  {label}{('  - ' + detail) if detail else ''}")

    with engine.connect() as connection:
        try:
            is_mssql = engine.dialect.name == "mssql"
            if is_mssql:
                database, offset = connection.execute(text("SELECT DB_NAME(), SYSDATETIMEOFFSET()")).one()
                lines.append(f"INFO  connected to database {database}; server time {offset}")
            inspector = inspect(connection)
            tables = set(inspector.get_table_names())
            for table, expected in EXPECTED_SCHEMA.items():
                if table not in tables:
                    check(False, f"{table}: table exists", "missing - this tool never creates it")
                    continue
                mapped = set(AuthBase.metadata.tables[table].columns.keys())
                check(mapped == set(expected), f"{table}: ORM maps exactly the expected columns")
                live = {column["name"]: column for column in inspector.get_columns(table)}
                check(set(live) == set(expected), f"{table}: live columns",
                      f"missing {sorted(set(expected) - set(live))}, extra {sorted(set(live) - set(expected))}"
                      if set(live) != set(expected) else f"{len(live)} columns")
                for name, (sql_type, length, nullable) in expected.items():
                    column = live.get(name)
                    if column is None:
                        continue
                    check(column["nullable"] == nullable, f"{table}.{name}: {'NULL' if nullable else 'NOT NULL'}",
                          "" if column["nullable"] == nullable else f"live is {'NULL' if column['nullable'] else 'NOT NULL'}")
                    if is_mssql:
                        live_type, live_length = _type_name(column["type"]), getattr(column["type"], "length", None)
                        type_ok = live_type == sql_type and (length is None or live_length == length)
                        check(type_ok, f"{table}.{name}: {sql_type}{f'({length})' if length else ''}",
                              "" if type_ok else f"live is {live_type}({live_length})")
                pk = inspector.get_pk_constraint(table).get("constrained_columns") or []
                check(pk == ["id"], f"{table}: primary key (id)", "" if pk == ["id"] else f"live is {pk}")
                if is_mssql:
                    identity = connection.execute(text(
                        "SELECT COLUMNPROPERTY(OBJECT_ID(:table), 'id', 'IsIdentity')"), {"table": table}).scalar()
                    check(identity == 1, f"{table}.id: IDENTITY")
            if "users" in tables:
                if is_mssql:
                    uniques = [index for index in _mssql_unique_indexes(connection, "users") if not index["primary"]]
                else:
                    uniques = [{"columns": u["column_names"], "filter": None, "constraint": True}
                               for u in inspector.get_unique_constraints("users")]
                    uniques += [{"columns": i["column_names"], "filter": None, "constraint": False}
                                for i in inspector.get_indexes("users") if i.get("unique")]
                for column in ("email", "mobile_no"):
                    matching = [u for u in uniques if u["columns"] == [column]]
                    check(bool(matching), f"users.{column}: UNIQUE")
                    if matching and is_mssql and not any(u["filter"] for u in matching):
                        lines.append(f"WARN  users.{column}: UNIQUE is not filtered - SQL Server treats NULLs as equal, "
                                     f"so only ONE user may have no {column}; a second one is refused on insert "
                                     "(a filtered unique index WHERE {column} IS NOT NULL avoids this - a schema "
                                     "change for the DBA, not made by this tool)")
            if "user_login_activity" in tables:
                fks = inspector.get_foreign_keys("user_login_activity")
                fk_ok = any(fk["constrained_columns"] == ["user_id"] and fk["referred_table"] == "users"
                            and fk["referred_columns"] == ["id"] for fk in fks)
                check(fk_ok, "user_login_activity.user_id -> users.id: FOREIGN KEY")
        finally:
            connection.rollback()  # read-only: nothing is ever committed
    return ok, lines


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage AlgoEdge dashboard users.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify-schema", help="read-only: compare the live auth tables with the ORM mapping")
    sub.add_parser("list-users", help="read-only")
    create = sub.add_parser("create-user")
    create.add_argument("--email")
    create.add_argument("--mobile")
    create.add_argument("--role", choices=auth_service.ROLES, default="USER")
    create.add_argument("--password-stdin", action="store_true")
    reset = sub.add_parser("set-password")
    reset.add_argument("--identifier", required=True, help="email or mobile number")
    reset.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args(argv)
    settings = get_settings()

    try:
        if args.command == "verify-schema":
            ok, lines = verify_schema(_engine())
            print("\n".join(lines))
            print(f"auth schema: {'PASS' if ok else 'FAIL'}")
            return 0 if ok else 1

        if args.command == "list-users":
            users = auth_service.list_users(_factory())
            for user in users:
                print(f"{user['id']:>4}  {user['role']:<5}  {'active' if user['isActive'] else 'DISABLED':<8}  "
                      f"{user['email'] or '-'}  {user['mobileNo'] or '-'}")
            print(f"{len(users)} user(s)")
            return 0

        if args.command == "create-user":
            try:
                user = auth_service.create_user(_factory(), email=args.email, mobile_no=args.mobile,
                                                password=_read_password(args.password_stdin), role=args.role,
                                                clock=_clock, min_length=settings.auth_password_min_length)
            except ValueError as error:
                raise SystemExit(str(error)) from error
            print(f"Created user {user['id']} ({user['role']}).")
            return 0

        identifier_type, identifier = auth_service.detect_identifier(args.identifier)
        if identifier_type is None:
            raise SystemExit("Not a valid email address or mobile number.")
        factory = _factory()
        with factory() as session:
            column = User.email if identifier_type == auth_service.EMAIL else User.mobile_no
            user_id = session.scalar(select(User.id).where(column == identifier))
        if user_id is None:
            raise SystemExit("No such user.")
        try:
            auth_service.set_user_password(factory, user_id, _read_password(args.password_stdin), actor_id=0,
                                           clock=_clock, min_length=settings.auth_password_min_length)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        print(f"Password updated for user {user_id}; any lock was cleared.")
        return 0
    except SQLAlchemyError as error:
        # Type only: driver messages can echo connection details.
        raise SystemExit(f"Database error ({type(error).__name__}).") from error


if __name__ == "__main__":
    raise SystemExit(main())
