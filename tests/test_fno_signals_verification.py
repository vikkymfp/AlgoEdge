import pytest
from growwapi.groww.exceptions import GrowwAPIException

from fno_signals import verification as verification_module
from fno_signals.verification import verify_order_status


class ScriptedClient:
    """Returns each response in `responses` in order, one per get_order_status call."""

    def __init__(self, responses: list) -> None:
        self._responses = iter(responses)
        self.calls: list[dict] = []

    def get_order_status(self, segment: str, groww_order_id: str):
        self.calls.append({"segment": segment, "groww_order_id": groww_order_id})
        response = next(self._responses)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(verification_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    return sleeps


def test_immediate_executed_is_success() -> None:
    client = ScriptedClient([{"order_status": "EXECUTED"}])

    result = verify_order_status(client, "gid-1", segment="FNO")

    assert result.outcome == "SUCCESS"
    assert result.order_status == "EXECUTED"
    assert result.attempts == 1


def test_success_captures_the_actual_fill_price() -> None:
    client = ScriptedClient([{"order_status": "EXECUTED", "average_fill_price": "142.35"}])

    result = verify_order_status(client, "gid-1", segment="FNO")

    assert result.average_fill_price == pytest.approx(142.35)


def test_success_falls_back_to_price_field_when_no_average_fill_price() -> None:
    client = ScriptedClient([{"order_status": "EXECUTED", "price": "100.0"}])

    result = verify_order_status(client, "gid-1", segment="FNO")

    assert result.average_fill_price == pytest.approx(100.0)


def test_success_fill_price_is_none_when_unavailable() -> None:
    client = ScriptedClient([{"order_status": "EXECUTED"}])

    result = verify_order_status(client, "gid-1", segment="FNO")

    assert result.average_fill_price is None


def test_completed_status_is_also_success() -> None:
    # COMPLETED is a real terminal status in Groww's enum, distinct from
    # EXECUTED, and must be treated as success too.
    client = ScriptedClient([{"order_status": "COMPLETED"}])

    result = verify_order_status(client, "gid-1", segment="FNO")

    assert result.outcome == "SUCCESS"


def test_delivery_awaited_status_is_also_success() -> None:
    client = ScriptedClient([{"order_status": "DELIVERY_AWAITED"}])

    result = verify_order_status(client, "gid-1", segment="FNO")

    assert result.outcome == "SUCCESS"


def test_rejected_captures_remark_as_reason() -> None:
    client = ScriptedClient([{"order_status": "REJECTED", "remark": "Insufficient margin"}])

    result = verify_order_status(client, "gid-1", segment="FNO")

    assert result.outcome == "FAILED"
    assert result.order_status == "REJECTED"
    assert result.reason == "Insufficient margin"


def test_failed_status_without_remark_uses_default_reason() -> None:
    client = ScriptedClient([{"order_status": "FAILED"}])

    result = verify_order_status(client, "gid-1", segment="FNO")

    assert result.outcome == "FAILED"
    assert "No reason provided" in result.reason


def test_cancelled_returns_explicit_cancellation_result() -> None:
    client = ScriptedClient([{"order_status": "CANCELLED", "remark": "User cancelled"}])

    result = verify_order_status(client, "gid-1", segment="FNO")

    assert result.outcome == "CANCELLED"
    assert result.reason == "User cancelled"


@pytest.mark.parametrize("status", ["NEW", "ACKED", "TRIGGER_PENDING", "APPROVED"])
def test_transitional_statuses_are_polled_until_execution(status, no_real_sleep) -> None:
    client = ScriptedClient([{"order_status": status}, {"order_status": "EXECUTED"}])

    result = verify_order_status(client, "gid-1", segment="FNO", initial_delay=0.5)

    assert result.outcome == "SUCCESS"
    assert result.attempts == 2
    assert len(client.calls) == 2
    assert no_real_sleep == [0.5]  # exactly one sleep, between the two polls


def test_exponential_backoff_doubles_each_transitional_attempt(no_real_sleep) -> None:
    client = ScriptedClient([{"order_status": "NEW"}] * 4 + [{"order_status": "EXECUTED"}])

    verify_order_status(client, "gid-1", segment="FNO", max_retries=5, initial_delay=0.5)

    assert no_real_sleep == [0.5, 1.0, 2.0, 4.0]


def test_exhausting_retries_returns_timeout() -> None:
    client = ScriptedClient([{"order_status": "NEW"}] * 5)

    result = verify_order_status(client, "gid-1", segment="FNO", max_retries=5, initial_delay=0.01)

    assert result.outcome == "TIMEOUT"
    assert result.attempts == 5
    assert result.order_status == "NEW"
    assert "did not reach a final state" in result.reason


def test_timeout_never_sleeps_after_the_final_attempt(no_real_sleep) -> None:
    client = ScriptedClient([{"order_status": "NEW"}] * 3)

    verify_order_status(client, "gid-1", segment="FNO", max_retries=3, initial_delay=1.0)

    assert len(no_real_sleep) == 2  # only between attempts, never after the last one


def test_transient_lookup_error_is_retried_not_treated_as_failure() -> None:
    client = ScriptedClient([
        GrowwAPIException(code="500", msg="temporary glitch"),
        {"order_status": "EXECUTED"},
    ])

    result = verify_order_status(client, "gid-1", segment="FNO", initial_delay=0.01)

    assert result.outcome == "SUCCESS"
    assert result.attempts == 2


def test_unrecognized_future_status_is_treated_as_transitional_not_a_crash() -> None:
    client = ScriptedClient([{"order_status": "SOME_NEW_STATUS_GROWW_ADDS_LATER"}, {"order_status": "EXECUTED"}])

    result = verify_order_status(client, "gid-1", segment="FNO", initial_delay=0.01)

    assert result.outcome == "SUCCESS"
    assert result.attempts == 2


def test_passes_segment_and_order_id_through_to_the_client() -> None:
    client = ScriptedClient([{"order_status": "EXECUTED"}])

    verify_order_status(client, "gid-42", segment="FNO")

    assert client.calls[0] == {"segment": "FNO", "groww_order_id": "gid-42"}


# -- partial fills ----------------------------------------------


def test_executed_with_fewer_filled_than_requested_is_partial() -> None:
    client = ScriptedClient([{"order_status": "EXECUTED", "filled_quantity": 50}])

    result = verify_order_status(client, "gid-1", segment="FNO", requested_quantity=100)

    assert result.outcome == "PARTIAL"
    assert result.requested_quantity == 100
    assert result.filled_quantity == 50
    assert result.remaining_quantity == 50
    assert "50 of 100" in result.reason


def test_executed_with_filled_equal_to_requested_is_full_success() -> None:
    client = ScriptedClient([{"order_status": "EXECUTED", "filled_quantity": 75}])

    result = verify_order_status(client, "gid-1", segment="FNO", requested_quantity=75)

    assert result.outcome == "SUCCESS"
    assert result.filled_quantity == 75
    assert result.remaining_quantity == 0


def test_executed_without_a_filled_quantity_field_is_never_assumed_partial() -> None:
    # Groww's response didn't include filled_quantity at all (unconfirmed
    # field name for this account, which has never had a real filled
    # order) - never guess it's a partial fill just because the field is
    # absent; fall back to assuming the full requested quantity filled,
    # same as before partial-fill tracking existed.
    client = ScriptedClient([{"order_status": "EXECUTED"}])

    result = verify_order_status(client, "gid-1", segment="FNO", requested_quantity=75)

    assert result.outcome == "SUCCESS"
    assert result.filled_quantity == 75
    assert result.remaining_quantity == 0


def test_remaining_quantity_prefers_the_brokers_own_value_when_present() -> None:
    client = ScriptedClient([{"order_status": "EXECUTED", "filled_quantity": 50, "remaining_quantity": 25}])

    result = verify_order_status(client, "gid-1", segment="FNO", requested_quantity=100)

    # Broker's own remaining_quantity (25) differs from the naive
    # 100-50=50 computation - the broker's own figure must win.
    assert result.remaining_quantity == 25


def test_cancelled_order_still_reports_whatever_partial_fill_occurred() -> None:
    client = ScriptedClient([{"order_status": "CANCELLED", "filled_quantity": 20, "remark": "partially filled then cancelled"}])

    result = verify_order_status(client, "gid-1", segment="FNO", requested_quantity=75)

    assert result.outcome == "CANCELLED"
    assert result.filled_quantity == 20


def test_timeout_still_reports_whatever_filled_quantity_was_last_observed() -> None:
    client = ScriptedClient([
        {"order_status": "NEW", "filled_quantity": 10},
        {"order_status": "NEW", "filled_quantity": 10},
    ])

    result = verify_order_status(client, "gid-1", segment="FNO", max_retries=2, initial_delay=0.01, requested_quantity=75)

    assert result.outcome == "TIMEOUT"
    assert result.filled_quantity == 10


# -- slippage tracking ----------------------------------------------


def test_slippage_is_computed_when_an_expected_price_is_known() -> None:
    client = ScriptedClient([{"order_status": "EXECUTED", "average_fill_price": 148.5}])

    result = verify_order_status(client, "gid-1", segment="FNO", expected_price=145.0)

    assert result.slippage == pytest.approx(3.5)


def test_negative_slippage_means_a_better_than_expected_fill() -> None:
    client = ScriptedClient([{"order_status": "EXECUTED", "average_fill_price": 140.0}])

    result = verify_order_status(client, "gid-1", segment="FNO", expected_price=145.0)

    assert result.slippage == pytest.approx(-5.0)


def test_slippage_is_none_when_no_expected_price_is_known() -> None:
    # The MARKET-order case: no pre-trade price exists on this account's
    # Groww tier, so slippage is genuinely uncomputable - never guessed as
    # zero.
    client = ScriptedClient([{"order_status": "EXECUTED", "average_fill_price": 148.5}])

    result = verify_order_status(client, "gid-1", segment="FNO")

    assert result.slippage is None


def test_slippage_is_none_when_fill_price_is_unknown() -> None:
    client = ScriptedClient([{"order_status": "EXECUTED"}])

    result = verify_order_status(client, "gid-1", segment="FNO", expected_price=145.0)

    assert result.slippage is None
