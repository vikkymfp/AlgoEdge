"""Phase 6 research protocol - the frozen configuration of the historical run.

RESEARCH ONLY. Nothing in src/ imports this module. It defines, in one place:
- the final-holdout boundary (research data ends 2024-04-25 23:59:59 IST, the
  holdout is 2024-04-26 .. 2025-04-25 and takes no part in any research
  decision);
- the two reviewed walk-forward designs, as explicit run configurations (the
  engine.walk_forward defaults are NOT changed by them);
- the 19 known non-standard session dates (tagged, never removed);
- a deterministic hash of the frozen 48-variant research grid;
- the metadata recorded with every protocol report.

The canonical strategy configuration is serialized straight from
fno_signals.config.strategy_config_for() - there is no second copy of it here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine, select

from fno_signals.config import INDEX_MAP, strategy_config_for
from research.phase6 import historical_db
from research.phase6.experiments import build_variants

PROTOCOL_VERSION = "phase6-protocol-1"  # bump whenever anything below changes meaning
REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------- holdout

HOLDOUT_START = date(2024, 4, 26)
HOLDOUT_END = date(2025, 4, 25)
RESEARCH_END_DATE = HOLDOUT_START - timedelta(days=1)  # 2024-04-25


def research_end_for(holdout_start: date) -> datetime:
    """Last bar_start research may read: the day before the holdout, 23:59:59
    (whole seconds - SQL Server DATETIME rounds 23:59:59.999 up to the next
    midnight, which would leak the holdout's first day)."""
    return datetime.combine(holdout_start - timedelta(days=1), time(23, 59, 59))


# ---------------------------------------------------------------- walk-forward designs


@dataclasses.dataclass(frozen=True)
class WalkForwardDesign:
    name: str
    train_days: int
    test_days: int
    step_days: int
    anchored: bool
    warmup_days: int
    train_exit_cutoff: bool
    min_train_trades: int

    @property
    def required_trading_days(self) -> int:
        """Fewest trading days that yield at least one window."""
        return self.warmup_days + self.train_days + self.test_days

    def kwargs(self) -> dict[str, Any]:
        """Arguments for engine.walk_forward (after df, trades, baseline name)."""
        return {"train_days": self.train_days, "test_days": self.test_days,
                "min_train_trades": self.min_train_trades, "step_days": self.step_days,
                "anchored": self.anchored, "warmup_days": self.warmup_days,
                "train_exit_cutoff": self.train_exit_cutoff}

    def as_dict(self) -> dict[str, Any]:
        return {**dataclasses.asdict(self), "required_trading_days": self.required_trading_days}


DESIGN_1_ROLLING = WalkForwardDesign(
    name="design1_rolling_250_63", train_days=250, test_days=63, step_days=63, anchored=False,
    warmup_days=10, train_exit_cutoff=True, min_train_trades=30,
)
DESIGN_2_ANCHORED = WalkForwardDesign(
    name="design2_anchored_500_250", train_days=500, test_days=250, step_days=250, anchored=True,
    warmup_days=10, train_exit_cutoff=True, min_train_trades=30,
)
PROTOCOL_DESIGNS = (DESIGN_1_ROLLING, DESIGN_2_ANCHORED)
DESIGNS_BY_NAME = {"design1": DESIGN_1_ROLLING, "design2": DESIGN_2_ANCHORED}

# ---------------------------------------------------------------- non-standard sessions

# master_5min.csv sessions that are not a normal 75-bar 09:15-15:25 day (short
# sessions and the two 22-bar DR-drill Saturdays), as found by validation and
# verified against the imported historical_candles. Tagged in reports, never
# removed from the primary dataset. 9 fall in the research period, 10 in the
# holdout.
NON_STANDARD_SESSIONS: tuple[date, ...] = tuple(date.fromisoformat(d) for d in (
    "2015-01-19", "2015-03-13", "2015-03-16", "2015-12-22", "2017-02-21", "2019-09-23", "2021-02-24",
    "2023-06-14", "2024-03-02", "2024-05-18", "2025-01-01", "2025-01-02", "2025-01-03", "2025-01-21",
    "2025-02-11", "2025-02-24", "2025-02-25", "2025-03-03", "2025-03-05",
))


def sessions_in(df: pd.DataFrame, sessions: tuple[date, ...] = NON_STANDARD_SESSIONS) -> list[date]:
    """The listed sessions that actually occur in this frame (IST dates)."""
    present = set(df.index.tz_convert("Asia/Kolkata").date) if len(df) else set()
    return [d for d in sessions if d in present]


def trades_entered_on(trades: list, sessions: tuple[date, ...] = NON_STANDARD_SESSIONS) -> list:
    """Trades whose IST entry date is one of `sessions` - for tagging now and
    for the later with/without-sessions sensitivity run."""
    wanted = set(sessions)
    return [t for t in trades if pd.Timestamp(t.entry_time).tz_convert("Asia/Kolkata").date() in wanted]


# ---------------------------------------------------------------- canonical serialization & grid hash


def _canonical(value: Any) -> Any:
    """Deterministic JSON-able form: dataclasses by field order, times ISO."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _canonical(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, time | date | datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(v) for v in value]
    return value  # ints, bools, None, str; floats stay numbers (json writes their exact repr)


def canonical_config(index_key: int) -> dict[str, Any]:
    """The production canonical strategy config for an index, serialized."""
    return _canonical(strategy_config_for(INDEX_MAP[index_key]))


def grid_payload(index_key: int) -> dict[str, Any]:
    variants, families = build_variants(strategy_config_for(INDEX_MAP[index_key]))
    return {"variants": [_canonical(v) for v in variants], "families": _canonical(families)}


def grid_hash(index_key: int) -> str:
    """SHA-256 of the exact, ordered research grid (every field of every
    variant, including its full strategy config, plus the family axes)."""
    blob = json.dumps(grid_payload(index_key), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- provenance


def git_state(repo: Path = REPO_ROOT) -> dict[str, Any]:
    """The real commit (never invented): None when git is unavailable."""
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                                check=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True,
                               check=True, timeout=10).stdout.strip() != ""
        return {"git_commit": commit, "git_worktree_dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"git_commit": None, "git_worktree_dirty": None}


def db_loads(engine: Engine, source: str) -> list[dict[str, Any]]:
    """Read-only: the historical_candle_loads rows for a source (load_id, SHA)."""
    loads = historical_db.HistoricalCandleLoad.__table__
    with engine.connect() as conn:
        rows = conn.execute(select(loads.c.id, loads.c.file_sha256).where(loads.c.source == source)
                            .order_by(loads.c.id)).all()
    return [{"load_id": row.id, "file_sha256": row.file_sha256} for row in rows]


def segment_summary(df: pd.DataFrame) -> dict[str, Any]:
    local = df.index.tz_convert("Asia/Kolkata")
    return {"first_bar": local[0].isoformat(), "last_bar": local[-1].isoformat(), "bars": len(df),
            "trading_days": len(set(local.date))}


def research_metadata(
    *,
    index_id: str,
    index_key: int,
    timeframe: str,
    dataset_source: str,
    segments: list[pd.DataFrame],
    designs: tuple[WalkForwardDesign, ...] | list[WalkForwardDesign],
    holdout_start: date | None,
    research_end: datetime | None,
    loads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from algoedge import __version__ as algoedge_version

    present = sorted({d for s in segments for d in sessions_in(s)})
    return {
        "protocol_version": PROTOCOL_VERSION,
        "algoedge_version": algoedge_version,
        **git_state(),
        "dataset_source": dataset_source,
        "db_loads": loads,  # [{load_id, file_sha256}] for DB-backed runs, else None
        "index_id": index_id,
        "timeframe": timeframe,
        "segments": [segment_summary(s) for s in segments],
        "research_end": research_end.isoformat() if research_end else None,
        "holdout_start": holdout_start.isoformat() if holdout_start else None,
        "holdout_end": HOLDOUT_END.isoformat() if holdout_start == HOLDOUT_START else None,
        "grid_hash": grid_hash(index_key),
        "grid_variants": len(grid_payload(index_key)["variants"]),
        "canonical_config": canonical_config(index_key),
        "walk_forward_designs": [d.as_dict() for d in designs],
        "non_standard_sessions_known": len(NON_STANDARD_SESSIONS),
        "non_standard_sessions_in_data": [d.isoformat() for d in present],
    }
