from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    """Per-trade brokerage/tax/fee rates for Net P&L (spec §27).

    Every field defaults to 0.0 - "not yet configured" rather than a
    guessed number. Groww's real F&O brokerage/STT/exchange-charge/GST/
    stamp-duty rates are precisely published, but this session has no
    verified, current source for them - encoding a wrong number would
    silently misstate P&L, which is worse than clearly showing "costs not
    configured." Configure real values via ALGOEDGE_COST_* env vars once
    you have your account's actual current rates.
    """

    brokerage_per_order: float = 0.0  # flat amount per executed leg (entry AND exit each count as one)
    stt_percent_on_sell: float = 0.0  # Securities Transaction Tax - options: charged on the sell leg's value
    exchange_charges_percent: float = 0.0  # on both legs' value
    gst_percent: float = 0.0  # on (brokerage + exchange charges)
    stamp_duty_percent_on_buy: float = 0.0  # on the buy leg's value only

    def is_configured(self) -> bool:
        return any([
            self.brokerage_per_order, self.stt_percent_on_sell, self.exchange_charges_percent,
            self.gst_percent, self.stamp_duty_percent_on_buy,
        ])


def compute_trade_costs(buy_price: float, sell_price: float, quantity: float, cost_model: CostModel) -> float:
    """Total brokerage+taxes+fees for one closed round-trip. `buy_price`/
    `sell_price` are whichever leg was actually a BUY vs a SELL - STT and
    stamp duty are transaction-side-specific (STT applies to selling,
    stamp duty to buying) regardless of whether that leg was the entry or
    the exit, so callers must map LONG/SHORT correctly rather than always
    treating entry_price as the buy leg.
    """
    buy_value = buy_price * quantity
    sell_value = sell_price * quantity
    brokerage = cost_model.brokerage_per_order * 2  # one charge per leg
    stt = sell_value * cost_model.stt_percent_on_sell / 100
    exchange = (buy_value + sell_value) * cost_model.exchange_charges_percent / 100
    stamp_duty = buy_value * cost_model.stamp_duty_percent_on_buy / 100
    gst = (brokerage + exchange) * cost_model.gst_percent / 100
    return brokerage + stt + exchange + stamp_duty + gst
