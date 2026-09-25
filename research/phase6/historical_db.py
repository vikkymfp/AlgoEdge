"""Phase 6 research: SQL Server tables for historical candle data.

RESEARCH / BACKTEST DATA ONLY. These tables hold historical OHLC candles and
the provenance of each import, for Phase 6 research and backtesting. Nothing
in src/ reads or writes them, and they play no part in signals, orders,
risk state or any trading decision.

Kept deliberately separate from production persistence:
- The models live on their own SQLAlchemy base (ResearchBase), never on
  algoedge.models.Base, so production `algoedge.db.init_db()` - which runs
  `Base.metadata.create_all()` at every dashboard start - never creates,
  alters or even knows about these tables.
- This module does not import algoedge.db or algoedge.models at all. It
  reuses the same configuration (algoedge.config.Settings: ALGOEDGE_DB_*)
  and the same SQL Server connection-string convention, so the tables land
  in the existing AlgoEdge database, dbo schema.
- It only creates tables that are missing (idempotent); it never creates the
  database itself, never alters or drops anything, and never touches
  production tables.

    PYTHONPATH=src:. python -m research.phase6.historical_db   # create the two tables

Timestamps follow the project convention: naive DATETIME2 holding IST
wall-clock time (callers re-localize to Asia/Kolkata on read). Prices follow
the project convention of FLOAT.
"""

from __future__ import annotations

from datetime import date, datetime
from urllib.parse import quote_plus

from sqlalchemy import (
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from algoedge.config import Settings, get_settings

# Known contaminated span of master_5min.csv (two interleaved candle series,
# see research/phase6 validation). Recorded here so the Step 3 loader can
# exclude it; nothing in this module imports data.
EXCLUDED_PERIODS: tuple[tuple[date, date], ...] = ((date(2015, 6, 22), date(2015, 11, 13)),)


class ResearchBase(DeclarativeBase):
    """Metadata for research-only tables. Never merged with algoedge.models.Base."""


class HistoricalCandle(ResearchBase):
    """One historical OHLC candle (research/backtest data only).

    bar_start/bar_end are IST wall-clock times. volume is NULL when the
    source has none - it is never invented. A source may hold each
    (index_id, timeframe, bar_start) only once."""

    __tablename__ = "historical_candles"
    __table_args__ = (
        UniqueConstraint("index_id", "timeframe", "bar_start", "source",
                         name="uq_historical_candles_index_timeframe_start_source"),
        Index("ix_historical_candles_backtest_lookup", "index_id", "timeframe", "bar_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    index_id: Mapped[str] = mapped_column(String(32), nullable=False)  # e.g. "nifty-50"
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)  # e.g. "5m"
    bar_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # IST wall-clock
    bar_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # IST wall-clock
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "master_5min.csv"
    load_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("historical_candle_loads.id", name="fk_historical_candles_load_id"), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class HistoricalCandleLoad(ResearchBase):
    """Provenance of one import into historical_candles (research only):
    which exact file (by SHA-256), how it validated, and what it covered.
    The same file content can only be registered once."""

    __tablename__ = "historical_candle_loads"
    __table_args__ = (
        UniqueConstraint("file_sha256", name="uq_historical_candle_loads_file_sha256"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    invalid_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    gap_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_bar: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_bar: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    validation_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


RESEARCH_TABLES = (HistoricalCandleLoad.__table__, HistoricalCandle.__table__)


def odbc_connection_url(settings: Settings, database: str | None = None) -> str:
    """Same convention as algoedge.db._odbc_connection_url (asserted equal in
    test_historical_db.py) - duplicated rather than imported so this module
    never loads the production models."""
    odbc_str = (
        f"DRIVER={{{settings.db_odbc_driver}}};"
        f"SERVER={settings.db_server};"
        f"DATABASE={database or settings.db_name};"
        "TrustServerCertificate=yes;"
    )
    if settings.db_trusted_connection:
        odbc_str += "Trusted_Connection=yes;"
    return f"mssql+pyodbc:///?odbc_connect={quote_plus(odbc_str)}"


def research_engine(settings: Settings | None = None) -> Engine:
    """Engine for the existing AlgoEdge database. Unlike production init_db(),
    a missing configuration is an error here - research loads must not
    silently do nothing."""
    settings = settings or get_settings()
    if not settings.db_server:
        raise RuntimeError("ALGOEDGE_DB_SERVER is not set - no database to create research tables in")
    # fast_executemany: pyodbc sends a bulk INSERT's parameter rows in one
    # round trip - what makes a ~200k-candle load practical.
    return create_engine(odbc_connection_url(settings), pool_pre_ping=True, fast_executemany=True)


def create_research_schema(engine: Engine | None = None) -> list[str]:
    """Creates historical_candle_loads and historical_candles if missing.

    Idempotent (checkfirst): existing tables are left exactly as they are,
    and only these two tables are ever considered - never the production
    ones. Returns the names of the tables that exist afterwards."""
    engine = engine or research_engine()
    ResearchBase.metadata.create_all(engine, tables=list(RESEARCH_TABLES), checkfirst=True)
    return [table.name for table in RESEARCH_TABLES]


if __name__ == "__main__":
    print("research tables ready:", ", ".join(create_research_schema()))
