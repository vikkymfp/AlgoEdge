from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MAX_SPREAD = 5.0


@dataclass(frozen=True)
class LiquidityCheckResult:
    allowed: bool
    reason: str
    spread: float | None = None


def check_liquidity(
    bid: float | None, ask: float | None, max_spread: float = DEFAULT_MAX_SPREAD,
) -> LiquidityCheckResult:
    """Blocks execution when an option's bid/ask spread is too wide to
    trade safely (spec §14's exact example: bid=100, ask=108, spread=8,
    max=5 -> blocked).

    When bid/ask aren't available at all, this is NOT treated as a block -
    it's an explicit "skipped" result. This is a real, deliberate
    limitation, not a shortcut: this account's Groww tier returns "Access
    forbidden" for get_quote/get_ltp/get_ohlc, so no caller in this
    codebase currently has a real bid/ask to pass in. Blocking every order
    because quotes are unavailable would make this check indistinguishable
    from a kill switch; returning "unsafe" would be a guess. "Skipped and
    allowed" is the only honest answer until the account's tier changes.
    """
    if bid is None or ask is None:
        return LiquidityCheckResult(
            True, "Bid/ask unavailable on this account's Groww tier - liquidity check skipped", spread=None,
        )
    spread = ask - bid
    if spread > max_spread:
        return LiquidityCheckResult(
            False, f"Spread {spread:.2f} exceeds the maximum allowed {max_spread:.2f}", spread=spread,
        )
    return LiquidityCheckResult(True, "Within the allowed spread", spread=spread)
