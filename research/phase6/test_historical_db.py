"""Tests for research.phase6.historical_db - SQLite only, no SQL Server needed.

    PYTHONPATH=src:. python -m pytest research/phase6/test_historical_db.py -q
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import DateTime, Float, Integer, String, Text, create_engine, insert, inspect
from sqlalchemy.exc import IntegrityError

from research.phase6 import historical_db as hdb

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def engine():
    engine = create_engine("sqlite:///:memory:")
    yield engine
    engine.dispose()


def _load_row(**overrides) -> dict:
    row = {"source": "master_5min.csv", "file_sha256": "a" * 64, "row_count": 10, "valid_rows": 10,
           "invalid_rows": 0, "duplicate_count": 0, "gap_count": 0}
    row.update(overrides)
    return row


def _candle(**overrides) -> dict:
    row = {"index_id": "nifty-50", "timeframe": "5m", "bar_start": datetime(2016, 1, 4, 9, 15),
           "bar_end": datetime(2016, 1, 4, 9, 20), "open": 7924.55, "high": 7937.55, "low": 7909.8,
           "close": 7915.2, "source": "master_5min.csv", "load_id": 1}
    row.update(overrides)
    return row


# ---------------- metadata ----------------


EXPECTED_CANDLE_COLUMNS = {
    # name: (type, nullable)
    "id": (Integer, False), "index_id": (String, False), "timeframe": (String, False),
    "bar_start": (DateTime, False), "bar_end": (DateTime, False), "open": (Float, False),
    "high": (Float, False), "low": (Float, False), "close": (Float, False), "volume": (Float, True),
    "source": (String, False), "load_id": (Integer, False), "created_at": (DateTime, False),
}
EXPECTED_LOAD_COLUMNS = {
    "id": (Integer, False), "source": (String, False), "file_sha256": (String, False),
    "row_count": (Integer, False), "valid_rows": (Integer, False), "invalid_rows": (Integer, False),
    "duplicate_count": (Integer, False), "gap_count": (Integer, False), "first_bar": (DateTime, True),
    "last_bar": (DateTime, True), "validation_json": (Text, True), "created_at": (DateTime, False),
}


@pytest.mark.parametrize("table, expected", [
    (hdb.HistoricalCandle.__table__, EXPECTED_CANDLE_COLUMNS),
    (hdb.HistoricalCandleLoad.__table__, EXPECTED_LOAD_COLUMNS),
])
def test_columns_types_and_nullability(table, expected) -> None:
    assert set(table.columns.keys()) == set(expected)
    for name, (sql_type, nullable) in expected.items():
        column = table.columns[name]
        assert isinstance(column.type, sql_type), name
        assert column.nullable is nullable, name


def test_string_lengths_follow_the_project_conventions() -> None:
    candles = hdb.HistoricalCandle.__table__.columns
    assert (candles.index_id.type.length, candles.timeframe.type.length, candles.source.type.length) == (32, 16, 64)
    loads = hdb.HistoricalCandleLoad.__table__.columns
    assert (loads.source.type.length, loads.file_sha256.type.length) == (64, 64)


def test_primary_keys_and_server_defaults() -> None:
    for table in hdb.RESEARCH_TABLES:
        assert [c.name for c in table.primary_key.columns] == ["id"]
        assert table.columns.id.autoincrement is True
        assert table.columns.created_at.server_default is not None
    assert hdb.HistoricalCandle.__table__.name == "historical_candles"
    assert hdb.HistoricalCandleLoad.__table__.name == "historical_candle_loads"
    assert all(table.schema is None for table in hdb.RESEARCH_TABLES)  # default schema = dbo on SQL Server


def test_candle_unique_constraint_and_lookup_index_are_declared() -> None:
    table = hdb.HistoricalCandle.__table__
    uniques = {c.name: [col.name for col in c.columns] for c in table.constraints if c.__class__.__name__ ==
               "UniqueConstraint"}
    assert uniques == {"uq_historical_candles_index_timeframe_start_source":
                       ["index_id", "timeframe", "bar_start", "source"]}
    indexes = {i.name: [col.name for col in i.columns] for i in table.indexes}
    assert indexes == {"ix_historical_candles_backtest_lookup": ["index_id", "timeframe", "bar_start"]}


def test_load_sha256_unique_constraint_is_declared() -> None:
    table = hdb.HistoricalCandleLoad.__table__
    uniques = {c.name: [col.name for col in c.columns] for c in table.constraints if c.__class__.__name__ ==
               "UniqueConstraint"}
    assert uniques == {"uq_historical_candle_loads_file_sha256": ["file_sha256"]}


# ---------------- created schema ----------------


def test_create_research_schema_creates_exactly_the_two_tables(engine) -> None:
    assert hdb.create_research_schema(engine) == ["historical_candle_loads", "historical_candles"]
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == {"historical_candles", "historical_candle_loads"}
    uniques = {u["name"]: u["column_names"] for u in inspector.get_unique_constraints("historical_candles")}
    assert uniques["uq_historical_candles_index_timeframe_start_source"] == [
        "index_id", "timeframe", "bar_start", "source"]
    index = {i["name"]: i["column_names"] for i in inspector.get_indexes("historical_candles")}
    assert index["ix_historical_candles_backtest_lookup"] == ["index_id", "timeframe", "bar_start"]


def test_create_research_schema_is_idempotent_and_keeps_existing_rows(engine) -> None:
    hdb.create_research_schema(engine)
    with engine.begin() as conn:
        conn.execute(insert(hdb.HistoricalCandleLoad), [_load_row()])
        conn.execute(insert(hdb.HistoricalCandle), [_candle()])
    hdb.create_research_schema(engine)
    hdb.create_research_schema(engine)
    with engine.connect() as conn:
        assert conn.execute(hdb.HistoricalCandle.__table__.select()).fetchall().__len__() == 1


def test_duplicate_candle_for_the_same_source_is_rejected(engine) -> None:
    hdb.create_research_schema(engine)
    with engine.begin() as conn:
        conn.execute(insert(hdb.HistoricalCandle), [_candle()])
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert(hdb.HistoricalCandle), [_candle(close=1.0)])
    # A different source, timeframe or start time is a different row.
    with engine.begin() as conn:
        conn.execute(insert(hdb.HistoricalCandle), [
            _candle(source="other.csv"), _candle(timeframe="15m"),
            _candle(bar_start=datetime(2016, 1, 4, 9, 20), bar_end=datetime(2016, 1, 4, 9, 25)),
        ])


def test_required_candle_fields_are_enforced_but_volume_may_be_null(engine) -> None:
    hdb.create_research_schema(engine)
    with engine.begin() as conn:
        conn.execute(insert(hdb.HistoricalCandle), [_candle(volume=None)])
    for field in ("index_id", "timeframe", "bar_start", "bar_end", "open", "high", "low", "close", "source",
                  "load_id"):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            # A distinct start time, so only the NULL (not the unique key) can fail it.
            row = {"bar_start": datetime(2016, 2, 1, 9, 15), field: None}
            conn.execute(insert(hdb.HistoricalCandle), [_candle(**row)])


def test_the_same_file_sha256_cannot_be_loaded_twice(engine) -> None:
    hdb.create_research_schema(engine)
    with engine.begin() as conn:
        conn.execute(insert(hdb.HistoricalCandleLoad), [_load_row(
            first_bar=datetime(2015, 1, 9, 9, 15), last_bar=datetime(2025, 4, 25, 15, 25),
            validation_json=json.dumps({"rows": 10}))])
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert(hdb.HistoricalCandleLoad), [_load_row(source="renamed.csv")])
    with engine.begin() as conn:
        conn.execute(insert(hdb.HistoricalCandleLoad), [_load_row(file_sha256="b" * 64)])


def test_load_optional_fields_may_be_null(engine) -> None:
    hdb.create_research_schema(engine)
    with engine.begin() as conn:
        conn.execute(insert(hdb.HistoricalCandleLoad), [_load_row()])
        row = conn.execute(hdb.HistoricalCandleLoad.__table__.select()).mappings().one()
    assert row["first_bar"] is None and row["last_bar"] is None and row["validation_json"] is None
    assert row["created_at"] is not None


# ---------------- separation from production ----------------


def test_research_base_is_separate_from_the_production_base() -> None:
    from algoedge.models import Base as ProductionBase

    assert hdb.ResearchBase is not ProductionBase
    assert set(hdb.ResearchBase.metadata.tables) == {"historical_candles", "historical_candle_loads"}
    assert not {"historical_candles", "historical_candle_loads"} & set(ProductionBase.metadata.tables)


def test_importing_the_module_does_not_import_production_persistence() -> None:
    code = (
        "import sys, research.phase6.historical_db\n"
        "print(sorted(m for m in ('algoedge.db', 'algoedge.models') if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                         env={"PYTHONPATH": f"{REPO / 'src'}:{REPO}", "PATH": "/usr/bin:/bin"}, check=True)
    assert out.stdout.strip() == "[]"


def test_production_init_db_creates_no_research_tables(tmp_path, monkeypatch) -> None:
    # Production init_db() runs create_all on its own Base; with the research
    # module imported, the tables it creates must still be production-only.
    from algoedge import db as db_module
    from algoedge.models import Base as ProductionBase

    engine = create_engine(f"sqlite:///{tmp_path / 'prod.db'}")
    ProductionBase.metadata.create_all(engine)
    tables = set(inspect(engine).get_table_names())
    assert not tables & {"historical_candles", "historical_candle_loads"}
    assert "orders" in tables and "strategy_signals" in tables
    assert db_module._engine is None  # importing/using the research module never initialised production DB
    engine.dispose()


def test_research_schema_never_touches_production_tables(tmp_path) -> None:
    from algoedge.models import Base as ProductionBase

    engine = create_engine(f"sqlite:///{tmp_path / 'shared.db'}")
    ProductionBase.metadata.create_all(engine)
    before = {t: [c["name"] for c in inspect(engine).get_columns(t)] for t in inspect(engine).get_table_names()}
    hdb.create_research_schema(engine)
    after = {t: [c["name"] for c in inspect(engine).get_columns(t)] for t in inspect(engine).get_table_names()}
    assert {t: after[t] for t in before} == before
    assert set(after) - set(before) == {"historical_candles", "historical_candle_loads"}
    engine.dispose()


# ---------------- configuration ----------------


def test_connection_url_matches_the_production_convention() -> None:
    from algoedge import db as db_module
    from algoedge.config import Settings

    for trusted in (True, False):
        settings = Settings(db_server="localhost\\SQLEXPRESS", db_name="AlgoEdge", db_trusted_connection=trusted)
        assert hdb.odbc_connection_url(settings) == db_module._odbc_connection_url(settings, "AlgoEdge")


def test_research_engine_requires_a_configured_server() -> None:
    from algoedge.config import Settings

    with pytest.raises(RuntimeError, match="ALGOEDGE_DB_SERVER"):
        hdb.research_engine(Settings(db_server=""))


def test_contaminated_period_is_recorded_for_the_loader() -> None:
    assert [(a.isoformat(), b.isoformat()) for a, b in hdb.EXCLUDED_PERIODS] == [("2015-06-22", "2015-11-13")]


# ---------------- foreign key ----------------


def test_load_id_is_a_foreign_key_to_the_load_table() -> None:
    fks = list(hdb.HistoricalCandle.__table__.columns.load_id.foreign_keys)
    assert len(fks) == 1
    assert fks[0].target_fullname == "historical_candle_loads.id"
    assert fks[0].constraint.name == "fk_historical_candles_load_id"


@pytest.fixture()
def fk_engine():
    from sqlalchemy import event

    engine = create_engine("sqlite:///:memory:")
    event.listen(engine, "connect", lambda dbapi_conn, _rec: dbapi_conn.execute("PRAGMA foreign_keys=ON"))
    yield engine
    engine.dispose()


def test_a_candle_must_reference_an_existing_load(fk_engine) -> None:
    hdb.create_research_schema(fk_engine)
    inspector = inspect(fk_engine)
    [fk] = inspector.get_foreign_keys("historical_candles")
    assert fk["referred_table"] == "historical_candle_loads" and fk["constrained_columns"] == ["load_id"]
    with pytest.raises(IntegrityError), fk_engine.begin() as conn:
        conn.execute(insert(hdb.HistoricalCandle), [_candle(load_id=999)])
    with fk_engine.begin() as conn:
        conn.execute(insert(hdb.HistoricalCandleLoad), [_load_row()])
        load_id = conn.execute(hdb.HistoricalCandleLoad.__table__.select()).mappings().one()["id"]
        conn.execute(insert(hdb.HistoricalCandle), [_candle(load_id=load_id)])


def test_research_engine_uses_fast_executemany(monkeypatch) -> None:
    from algoedge.config import Settings

    captured = {}
    monkeypatch.setattr(hdb, "create_engine", lambda url, **kw: captured.update(kw) or "engine")
    assert hdb.research_engine(Settings(db_server="localhost")) == "engine"
    assert captured["fast_executemany"] is True and captured["pool_pre_ping"] is True
