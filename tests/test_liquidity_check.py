from algoedge.liquidity_check import check_liquidity


def test_spread_within_limit_is_allowed() -> None:
    result = check_liquidity(bid=100.0, ask=103.0, max_spread=5.0)

    assert result.allowed is True
    assert result.spread == 3.0


def test_spread_over_limit_is_blocked() -> None:
    # The spec's own worked example: bid=100, ask=108, spread=8, max=5.
    result = check_liquidity(bid=100.0, ask=108.0, max_spread=5.0)

    assert result.allowed is False
    assert result.spread == 8.0
    assert "exceeds" in result.reason.lower()


def test_spread_exactly_at_limit_is_allowed() -> None:
    result = check_liquidity(bid=100.0, ask=105.0, max_spread=5.0)

    assert result.allowed is True
    assert result.spread == 5.0


def test_missing_bid_is_skipped_not_blocked() -> None:
    result = check_liquidity(bid=None, ask=108.0, max_spread=5.0)

    assert result.allowed is True
    assert result.spread is None
    assert "unavailable" in result.reason.lower()


def test_missing_ask_is_skipped_not_blocked() -> None:
    result = check_liquidity(bid=100.0, ask=None, max_spread=5.0)

    assert result.allowed is True
    assert result.spread is None


def test_both_missing_is_skipped_not_blocked() -> None:
    result = check_liquidity(bid=None, ask=None)

    assert result.allowed is True
    assert result.spread is None


def test_uses_the_default_max_spread_when_not_given() -> None:
    result = check_liquidity(bid=100.0, ask=110.0)

    assert result.allowed is False  # spread 10 > default 5
