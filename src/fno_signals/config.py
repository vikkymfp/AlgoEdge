from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time


@dataclass(frozen=True)
class SignalConfig:
    """Mirrors the Pine Script's "Signal" input group."""

    ema_fast_length: int = 9
    ema_slow_length: int = 21
    rsi_length: int = 14
    rsi_bull: float = 55.0  # rsiBull — above this, RSI is on the Call side
    rsi_bear: float = 45.0  # rsiBear — below this, RSI is on the Put side
    supertrend_length: int = 10  # stLen — ATR length used *only* by Supertrend
    supertrend_multiplier: float = 3.0  # stMult
    # useVwap. Defaults OFF here (Pine defaults it on): yfinance reports
    # Volume=0 for index tickers (^NSEI/^NSEBANK/^BSESN), so session_vwap()
    # is always NaN for these feeds, and Pine's na-comparison-is-false
    # semantics mean useVwap=True would silently block every signal. Set it
    # True only when feeding an instrument with real traded volume.
    use_vwap: bool = False


@dataclass(frozen=True)
class RiskConfig:
    """Mirrors the Pine Script's "Risk" input group.

    All distances are measured on the underlying's price, not the option
    premium — the Pine script computes SL/target on the chart price and
    only uses them to size the option trade's boundaries.
    """

    atr_length: int = 14  # atrLen — separate from supertrend_length
    sl_multiplier: float = 1.5  # slMult
    tp_multiplier: float = 4.5  # tpMult (keeps a 1:3 R:R with the defaults)
    min_sl_points: float = 0.0  # minSlPoints, 0 = off


@dataclass(frozen=True)
class OptionConfig:
    """Mirrors the Pine Script's "Option" input group."""

    strike_step: int = 50  # Nifty 50 = 50; Bank Nifty / Sensex = 100


@dataclass(frozen=True)
class SessionConfig:
    """Mirrors the Pine Script's "Time filter (IST)" input group.

    Entries are restricted to this window. Open trades are NEVER force-closed
    at session end — they carry over to subsequent days and exit only on
    their own stop loss or target (delivery / carry-forward style).
    """

    start: time = time(9, 15)
    end: time = time(15, 40)
    timezone: str = "Asia/Kolkata"


@dataclass(frozen=True)
class StrategyConfig:
    signal: SignalConfig = field(default_factory=SignalConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    option: OptionConfig = field(default_factory=OptionConfig)
    session: SessionConfig = field(default_factory=SessionConfig)


DEFAULT_CONFIG = StrategyConfig()


@dataclass(frozen=True)
class IndexConfig:
    """Per-underlying parameters that differ across the three supported indices."""

    name: str  # full display name, used as the option symbol's underlying label
    ticker: str  # yfinance ticker, for spot/candle data
    strike_step: int
    # Reference lot size only — NOT used for live order quantity. Exchange
    # lot sizes are revised periodically (confirmed live: NIFTY is actually
    # 65 as of this writing, not 75), so broker.execute_market_order()
    # always uses the LIVE lot size from the resolved contract instead.
    lot_size: int
    exchange: str  # Groww exchange code for this underlying's OPTIONS listing
    groww_underlying: str  # exact underlying_symbol in Groww's instrument
    # master — NOT the display name (e.g. "NIFTY", never "NIFTY 50"/"NIFTY50")


# Menu/CLI choice number -> IndexConfig. The numbers are the contract for
# both --index on the command line and the interactive menu.
#
# exchange/groww_underlying were verified live against Groww's own
# get_all_instruments() instrument master, not assumed: NIFTY and BANK
# NIFTY options list on NSE, but SENSEX options list on BSE, not NSE.
INDEX_MAP: dict[int, IndexConfig] = {
    1: IndexConfig(name="NIFTY 50", ticker="^NSEI", strike_step=50, lot_size=75, exchange="NSE", groww_underlying="NIFTY"),
    2: IndexConfig(name="BANK NIFTY", ticker="^NSEBANK", strike_step=100, lot_size=30, exchange="NSE", groww_underlying="BANKNIFTY"),
    3: IndexConfig(name="SENSEX", ticker="^BSESN", strike_step=100, lot_size=20, exchange="BSE", groww_underlying="SENSEX"),
}


def strategy_config_for(index_config: IndexConfig) -> StrategyConfig:
    """Builds a StrategyConfig with the chosen index's strike step wired in."""
    return StrategyConfig(
        signal=DEFAULT_CONFIG.signal,
        risk=DEFAULT_CONFIG.risk,
        option=OptionConfig(strike_step=index_config.strike_step),
        session=DEFAULT_CONFIG.session,
    )
