"""Read-only SQL Server evidence extractor for the Phase 8.1 campaign.

Exports the paper engine's existing records for a time range - no table is
created or altered (it never calls algoedge.db.init_db() or create_all()),
and only SELECT statements built with SQLAlchemy Core bind parameters are
executed. Every statement is checked again before execution and the
connection is always rolled back, never committed.

Output: one directory per extraction holding a deterministic JSON Lines file
per table (rows ordered by id, keys sorted, timestamps as written by the
database - naive, server local time; protocol assumption A3 is IST),
baseline.json (the state in force at the range start) and manifest.json
(server/database identifier without credentials, extraction time, range,
row counts, the exact SQL and parameters, file SHA-256s).

Connection (no credentials are ever written to output):
- `--db-url-env NAME`: a SQLAlchemy URL taken from environment variable NAME;
- otherwise the dashboard's own ALGOEDGE_DB_* settings (same ODBC URL the
  app builds, algoedge.db._odbc_connection_url).

    PYTHONPATH=src:. python -m research.phase8.tools.extract --start 2026-10-01T09:00 --end 2026-10-01T16:00 \
        --out research/phase8/evidence/<campaign>/db
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus

from sqlalchemy import bindparam, create_engine, event, select
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.sql import Select

from algoedge.models import (
    AlertEvent,
    AutoTradeAccountSnapshot,
    OrderRecord,
    PaperDecisionEvent,
    RiskStateEvent,
    StrategySignal,
)
from research.phase8.tools.bars import INDEX_TICKERS
from research.phase8.tools.common import (
    IST,
    canonical_json,
    new_run_id,
    redact,
    sha256_file,
    stamp,
    utc_now,
)

SCHEMA = "phase8.db_extract.v1"
# The protocol's evidence tables, in export order.
MODELS = {
    "strategy_signals": StrategySignal,
    "orders": OrderRecord,
    "risk_state_events": RiskStateEvent,
    "auto_trade_account_snapshots": AutoTradeAccountSnapshot,
    "paper_decision_events": PaperDecisionEvent,
    "alert_events": AlertEvent,
}
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|GRANT|REVOKE|DENY|"
    r"INTO|BACKUP|RESTORE|DBCC|SHUTDOWN|KILL|RECONFIGURE|SP_\w+|XP_\w+)\b", re.IGNORECASE)


class ReadOnlyViolation(RuntimeError):
    """A statement other than a plain SELECT was about to be executed."""


def assert_select_only(sql: str) -> None:
    stripped = sql.strip()
    if not re.match(r"(?is)^select\b", stripped):
        raise ReadOnlyViolation(f"not a SELECT statement: {stripped[:60]!r}")
    if ";" in stripped.rstrip(";"):
        raise ReadOnlyViolation("multiple statements are not allowed")
    match = _FORBIDDEN.search(stripped)
    if match:
        raise ReadOnlyViolation(f"forbidden keyword {match.group(0)!r} in statement")


def columns_of(table: str) -> list[str]:
    return [column.name for column in MODELS[table].__table__.columns]


def range_query(table: str) -> Select:
    """Every row created in [start, end), ordered by id; start/end are bind
    parameters."""
    model_table = MODELS[table].__table__
    created = model_table.c.created_at
    return (select(*[model_table.c[name] for name in columns_of(table)])
            .where(created >= bindparam("start"), created < bindparam("end"))
            .order_by(model_table.c.id))


def baseline_queries() -> dict[str, Select]:
    """The state in force at the range start: each index's last account
    snapshot and the last paper risk-state row before `start`."""
    snapshots = MODELS["auto_trade_account_snapshots"].__table__
    risk = MODELS["risk_state_events"].__table__
    queries = {
        f"account_snapshot:{index_id}": (
            select(*[snapshots.c[name] for name in columns_of("auto_trade_account_snapshots")])
            .where(snapshots.c.index_id == bindparam(f"index_{index_id.replace('-', '_')}", index_id),
                   snapshots.c.created_at < bindparam("start"))
            .order_by(snapshots.c.id.desc()).limit(1))
        for index_id in sorted(INDEX_TICKERS)
    }
    queries["risk_state"] = (
        select(*[risk.c[name] for name in columns_of("risk_state_events")])
        .where(risk.c.scope == bindparam("scope", "paper"), risk.c.created_at < bindparam("start"))
        .order_by(risk.c.id.desc()).limit(1))
    return queries


def install_guard(engine: Engine) -> None:
    """Re-checks every statement this tool sends (tagged with the
    phase8_read_only execution option) at the cursor level."""

    @event.listens_for(engine, "before_cursor_execute")
    def _guard(_conn, _cursor, statement, _parameters, context, _executemany):  # noqa: ANN001
        if context is not None and context.execution_options.get("phase8_read_only"):
            assert_select_only(statement)


def _execute(connection: Connection, query: Select, params: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    compiled = query.compile(dialect=connection.dialect)
    sql = str(compiled)
    assert_select_only(sql)
    # Tagged per statement (Connection.execution_options would tag the whole connection).
    result = connection.execute(query.execution_options(phase8_read_only=True), params)
    return sql, [dict(row._mapping) for row in result]


def safe_db_identifier(url: str) -> dict[str, str | None]:
    """Backend, server and database names only - never user or password."""
    parsed = make_url(url)
    server = parsed.host
    database = parsed.database
    odbc = parsed.query.get("odbc_connect")
    if odbc:
        text = unquote_plus(odbc if isinstance(odbc, str) else odbc[0])
        fields = {k.strip().upper(): v.strip() for k, _, v in
                  (part.partition("=") for part in text.split(";") if "=" in part)}
        server = fields.get("SERVER", server)
        database = fields.get("DATABASE", database)
    return {"backend": parsed.get_backend_name(), "server": server,
            "database": database.rsplit("/", 1)[-1] if database else None}


def resolve_db_url(db_url_env: str | None) -> str:
    if db_url_env:
        value = os.environ.get(db_url_env)
        if not value:
            raise RuntimeError(f"environment variable {db_url_env} is not set")
        return value
    from algoedge.config import get_settings
    from algoedge.db import _odbc_connection_url  # pure URL builder, no side effects

    settings = get_settings()
    if not settings.db_server:
        raise RuntimeError("ALGOEDGE_DB_SERVER is not set - no database configured")
    return _odbc_connection_url(settings, settings.db_name)


def make_engine(url: str) -> Engine:
    engine = create_engine(url, pool_pre_ping=True)
    install_guard(engine)
    return engine


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    return {key: (value.isoformat() if isinstance(value, datetime) else value) for key, value in row.items()}


def _code_commit() -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10,
                                check=True, cwd=Path(__file__).resolve().parent)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def extract(engine: Engine, db_url: str, start: datetime, end: datetime, out_dir: Path, *,
            tables: Iterable[str] = tuple(MODELS), run_id: str | None = None,
            clock=utc_now) -> Path:
    """Writes one extraction directory and returns it. `start`/`end` are
    naive database-local timestamps (IST under assumption A3)."""
    if start.tzinfo is not None or end.tzinfo is not None:
        raise ValueError("start/end must be naive database-local (IST) timestamps")
    if end <= start:
        raise ValueError("end must be after start")
    table_names = list(tables)
    unknown = [name for name in table_names if name not in MODELS]
    if unknown:
        raise ValueError(f"unknown tables: {unknown}")
    run_id = run_id or new_run_id()
    extracted_at = clock()
    target = out_dir / f"extract_{extracted_at.astimezone(IST):%Y%m%dT%H%M%S%z}_{run_id}"
    target.mkdir(parents=True, exist_ok=False)
    params = {"start": start, "end": end}
    manifest: dict[str, Any] = {
        "schema": SCHEMA, "run_id": run_id, "extracted_at": stamp(extracted_at),
        "database": safe_db_identifier(db_url), "code_commit": _code_commit(),
        "range": {"start": start.isoformat(), "end": end.isoformat(), "end_exclusive": True,
                  "timestamp_basis": "naive database-local time (protocol assumption A3: IST)"},
        "scope": "all rows of each table created in range; no source/index filter",
        "table_order": table_names, "tables": {}, "baseline": {},
    }
    with engine.connect() as connection:
        try:
            for table in table_names:
                sql, rows = _execute(connection, range_query(table), params)
                path = target / f"{table}.jsonl"
                with path.open("x", encoding="utf-8") as handle:
                    for row in rows:
                        handle.write(canonical_json(_serialize(row)) + "\n")
                manifest["tables"][table] = {
                    "file": path.name, "rows": len(rows), "columns": columns_of(table),
                    "sql": sql, "params": {k: v.isoformat() for k, v in params.items()},
                    "sha256": sha256_file(path),
                }
            baseline: dict[str, Any] = {}
            for name, query in baseline_queries().items():
                sql, rows = _execute(connection, query, {"start": start})
                baseline[name] = _serialize(rows[0]) if rows else None
                manifest["baseline"][name] = {"sql": sql, "rows": len(rows)}
        finally:
            connection.rollback()  # read-only: nothing is ever committed
    baseline_path = target / "baseline.json"
    baseline_path.write_text(canonical_json(baseline) + "\n", encoding="utf-8")
    manifest["baseline_file"] = {"file": baseline_path.name, "sha256": sha256_file(baseline_path)}
    (target / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    return target


def _parse_local(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    if value.tzinfo is not None:
        value = value.astimezone(IST).replace(tzinfo=None)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", required=True, help="IST, e.g. 2026-10-01T09:00")
    parser.add_argument("--end", required=True, help="IST, exclusive")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--db-url-env", default=None, help="env var holding a SQLAlchemy URL")
    args = parser.parse_args(argv)
    url = resolve_db_url(args.db_url_env)
    engine = make_engine(url)
    try:
        print(extract(engine, url, _parse_local(args.start), _parse_local(args.end), args.out))
    except Exception as error:  # noqa: BLE001 - report without leaking the connection string
        print(f"EXTRACTION FAILED: {type(error).__name__}: {redact(str(error))}")
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
