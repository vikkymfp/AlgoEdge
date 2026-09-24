import pytest

from algoedge import alerts
from algoedge import db as db_module


@pytest.fixture(autouse=True)
def reset_db_module_state():
    db_module._engine = None
    db_module._session_factory = None
    yield
    db_module._engine = None
    db_module._session_factory = None


def test_raise_alert_never_raises_when_db_unavailable() -> None:
    alerts.raise_alert(alerts.KILL_SWITCH_ACTIVATED, "test message", source="test")


def test_raise_alert_persists_with_the_correct_severity(monkeypatch) -> None:
    recorded = []
    monkeypatch.setattr(
        db_module, "record_alert_event",
        lambda **kwargs: recorded.append(kwargs),
    )

    alerts.raise_alert(alerts.KILL_SWITCH_ACTIVATED, "engaged manually", source="web_server")

    assert recorded == [{
        "severity": "CRITICAL", "category": alerts.KILL_SWITCH_ACTIVATED,
        "message": "engaged manually", "source": "web_server",
    }]


def test_order_rejected_is_a_warning(monkeypatch) -> None:
    recorded = []
    monkeypatch.setattr(db_module, "record_alert_event", lambda **kwargs: recorded.append(kwargs))

    alerts.raise_alert(alerts.ORDER_REJECTED, "rejected by broker", source="fno_signals")

    assert recorded[0]["severity"] == "WARNING"


def test_system_restart_is_informational(monkeypatch) -> None:
    recorded = []
    monkeypatch.setattr(db_module, "record_alert_event", lambda **kwargs: recorded.append(kwargs))

    alerts.raise_alert(alerts.SYSTEM_RESTART, "dashboard started", source="web_server")

    assert recorded[0]["severity"] == "INFO"


def test_every_category_has_a_defined_severity() -> None:
    categories = [
        alerts.ORDER_REJECTED, alerts.ORDER_FAILED, alerts.BROKER_DISCONNECTED,
        alerts.POSITION_MISMATCH, alerts.DAILY_LOSS_LIMIT_REACHED, alerts.KILL_SWITCH_ACTIVATED,
        alerts.UNEXPECTED_POSITION, alerts.UNEXPECTED_ORDER, alerts.WEBHOOK_AUTH_FAILURE,
        alerts.DATABASE_FAILURE, alerts.TRADING_HALTED, alerts.SYSTEM_RESTART,
    ]
    for category in categories:
        assert category in alerts.SEVERITY
