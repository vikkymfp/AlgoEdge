"""Phase 8.0: evidence plumbing, the status/alert collector and the
concurrency drill helper - all against mocked HTTP."""

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from research.phase8.tools import collector, drill
from research.phase8.tools.common import (
    EvidenceFile,
    canonical_json,
    encode_number,
    read_jsonl,
    redact,
    validate_base_url,
    verify_record,
)

T0 = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)  # 09:30 IST


class FakeClock:
    def __init__(self, start=T0):
        self.now = start
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self.now

    def sleep(self, seconds):
        with self._lock:
            self.now += timedelta(seconds=seconds)


STATUS = {
    "enabled": True, "killSwitch": False, "killSwitchReason": None, "tradesToday": 3, "entriesToday": 2,
    "realizedPnlToday": 12.5, "realizedPnlTodayUnit": "UNDERLYING_POINTS", "consecutiveLosses": 0,
    "consecutiveLossHalt": False, "lastExitAt": None,
    "limits": {"dailyLossLimit": 5000.0, "dailyLossLimitUnit": "UNDERLYING_POINTS", "maxTradesPerDay": 10,
               "maxTradesPerDayCounts": "NEW_ENTRY_FILLS", "maxOpenPositions": 1, "maxQuantity": 50,
               "tradingStart": "09:15:00", "tradingEnd": "15:30:00", "entryCutoff": "15:00:00",
               "squareOffTime": "15:20:00", "maxConsecutiveLosses": 3, "cooldownMinutes": 5},
    "accounts": {
        "nifty-50": {"indexName": "NIFTY 50", "cash": 1.0, "quantity": 1, "averagePrice": 100.0, "side": "CALL"},
        "sensex": {"indexName": "SENSEX", "cash": 1.0, "quantity": 0, "averagePrice": None, "side": None},
    },
    "scheduler": {"tickSeconds": 300.0, "indexIds": ["nifty-50", "sensex"]},
}


def transport_returning(responses, calls=None):
    def transport(method, url, timeout):
        if calls is not None:
            calls.append((method, url))
        for suffix, response in responses.items():
            if url.split("?")[0].endswith(suffix):
                if isinstance(response, Exception):
                    raise response
                status, body = response
                return status, body if isinstance(body, str) else json.dumps(body)
        raise AssertionError(f"unexpected request {method} {url}")
    return transport


# ---------------- common ----------------


def test_redact_removes_credentials() -> None:
    text = "login failed PWD=hunter2; token: abc.def Authorization=Bearer xyz mssql://user:pw@host/db"
    cleaned = redact(text)
    for secret in ("hunter2", "abc.def", "xyz", "user:pw"):
        assert secret not in cleaned


def test_non_finite_numbers_are_recorded_not_replaced() -> None:
    assert [encode_number(v) for v in (float("nan"), float("inf"), -float("inf"), 1.5, None)] == \
        ["NaN", "Infinity", "-Infinity", 1.5, None]
    assert canonical_json({"b": float("nan"), "a": 1}) == '{"a":1,"b":"NaN"}'


def test_evidence_files_are_exclusive_and_self_hashed(tmp_path) -> None:
    with EvidenceFile(tmp_path, "status", "run1", T0) as evidence:
        evidence.append({"x": 1})
    with pytest.raises(FileExistsError):
        EvidenceFile(tmp_path, "status", "run1", T0)  # never reopened or overwritten
    (record,) = read_jsonl(evidence.path)
    assert verify_record(record)
    record["x"] = 2
    assert not verify_record(record)


@pytest.mark.parametrize("url", ["ftp://127.0.0.1", "http://u:p@127.0.0.1:5173", "http://127.0.0.1:5173/api",
                                 "http://example.com:5173"])
def test_base_url_is_validated(url) -> None:
    with pytest.raises(ValueError):
        validate_base_url(url)


def test_non_loopback_needs_an_explicit_opt_in() -> None:
    assert validate_base_url("http://10.0.0.5:5173", allow_non_loopback=True) == "http://10.0.0.5:5173"


# ---------------- collector ----------------


def test_a_normal_sample_extracts_the_protocol_fields() -> None:
    calls = []
    transport = transport_returning({"/api/auto-trading/status": (200, STATUS),
                                     "/api/alerts": (200, {"alerts": [{"id": 1}]})}, calls)
    record = collector.collect_sample(transport, "http://127.0.0.1:5173", run_id="r", seq=0, timeout=5,
                                      alerts_limit=50, clock=FakeClock())
    assert record["ok"] and record["problems"] == []
    extracted = record["status"]["extracted"]
    assert (extracted["entriesToday"], extracted["tradesToday"]) == (2, 3)
    assert extracted["accounts"]["nifty-50"]["side"] == "CALL"
    assert extracted["openPositions"] == {"count": 1, "indexIds": ["nifty-50"], "reason": None}
    assert record["alerts"]["rows"] == [{"id": 1}]
    assert record["collected_at"]["ist"].endswith("+05:30")
    assert {method for method, _ in calls} == {"GET"}  # read-only
    assert [url.split("?")[0] for _, url in calls] == ["http://127.0.0.1:5173/api/auto-trading/status",
                                                        "http://127.0.0.1:5173/api/alerts"]


def test_missing_fields_are_named_never_defaulted() -> None:
    body = json.loads(json.dumps(STATUS))
    del body["entriesToday"]
    del body["accounts"]["sensex"]["quantity"]
    transport = transport_returning({"/api/auto-trading/status": (200, body), "/api/alerts": (200, {"alerts": []})})
    record = collector.collect_sample(transport, "http://127.0.0.1:5173", run_id="r", seq=0, timeout=5,
                                      alerts_limit=50, clock=FakeClock())
    assert not record["ok"]
    assert set(record["status"]["missing_fields"]) == {"entriesToday", "accounts.sensex.quantity"}
    assert "entriesToday" not in record["status"]["extracted"]
    assert record["status"]["extracted"]["openPositions"]["count"] is None  # no partial count


@pytest.mark.parametrize("response, problem", [
    (ConnectionRefusedError("refused"), "status request failed: ConnectionRefusedError"),
    ((500, {"detail": "boom"}), "status HTTP 500"),
    ((200, "<html>not json</html>"), "status body is not JSON"),
])
def test_api_failures_are_recorded_explicitly(response, problem) -> None:
    transport = transport_returning({"/api/auto-trading/status": response, "/api/alerts": (200, {"alerts": []})})
    record = collector.collect_sample(transport, "http://127.0.0.1:5173", run_id="r", seq=0, timeout=5,
                                      alerts_limit=50, clock=FakeClock())
    assert not record["ok"] and problem in record["problems"]
    assert record["status"]["extracted"] == {}


def test_the_run_loop_samples_on_a_fixed_interval(tmp_path) -> None:
    clock = FakeClock()
    transport = transport_returning({"/api/auto-trading/status": (200, STATUS), "/api/alerts": (200, {"alerts": []})})
    path = collector.run(tmp_path, samples=3, transport=transport, clock=clock, sleep=clock.sleep, run_id="r1")
    records = read_jsonl(path)
    assert [r["seq"] for r in records] == [0, 1, 2]
    assert [r["collected_at"]["utc"] for r in records] == [
        (T0 + timedelta(seconds=60 * k)).isoformat() for k in range(3)]
    assert all(verify_record(r) and r["interval_seconds"] == collector.DEFAULT_INTERVAL_SECONDS for r in records)


# ---------------- drill ----------------


def test_the_drill_only_posts_the_paper_cycle_with_quantity_one(tmp_path) -> None:
    calls = []
    transport = transport_returning({f"/api/auto-trading/run/{i}": (200, {"order": None})
                                     for i in ("nifty-50", "sensex", "bank-nifty")}, calls)
    run = drill.Drill(tmp_path, mode="repeat", transport=transport, clock=FakeClock(), run_id="d1")
    drill.run_repeat(run, ["nifty-50", "sensex"], rounds=2, concurrency=2, pause=0)
    run.close()
    assert len(calls) == 8
    assert {method for method, _ in calls} == {"POST"}
    assert all(url.endswith("?interval=5m&quantity=1") and "/api/auto-trading/run/" in url for _, url in calls)


def test_409_and_errors_are_preserved_as_evidence(tmp_path) -> None:
    responses = {"/api/auto-trading/run/nifty-50": (409, {"detail": "Another paper cycle for nifty-50 is busy"}),
                 "/api/auto-trading/run/sensex": ConnectionResetError("reset")}
    run = drill.Drill(tmp_path, mode="repeat", transport=transport_returning(responses), clock=FakeClock(),
                      run_id="d2")
    run.fire(0, ["nifty-50", "sensex"])
    summary = run.close()
    records = [r for r in read_jsonl(run.evidence.path) if "index_id" in r]
    by_index = {r["index_id"]: r for r in records}
    assert by_index["nifty-50"]["http_status"] == 409
    assert by_index["nifty-50"]["body_json"]["detail"].startswith("Another paper cycle")
    assert by_index["sensex"]["http_status"] is None and by_index["sensex"]["error"]["type"] == "ConnectionResetError"
    assert summary["by_status"] == {"409": 1, "ConnectionResetError": 1}
    assert len({r["request_id"] for r in records}) == 2 and all(r["run_id"] == "d2" for r in records)
    assert all(r["sent_at"]["utc"] and r["received_at"]["utc"] for r in records)


def test_requests_in_a_round_are_released_together(tmp_path) -> None:
    arrived = []
    gate = threading.Barrier(3, timeout=5)

    def transport(method, url, timeout):
        arrived.append(url)
        gate.wait()  # only returns if all three are in flight at once
        return 200, "{}"

    run = drill.Drill(tmp_path, mode="repeat", transport=transport, clock=FakeClock(), run_id="d3")
    run.fire(0, ["nifty-50", "sensex", "bank-nifty"])
    run.close()
    assert len(arrived) == 3


def test_overlap_fires_offset_seconds_after_each_scheduler_tick(tmp_path) -> None:
    clock = FakeClock(T0 + timedelta(seconds=10))
    fired = []
    transport = transport_returning({"/api/auto-trading/run/nifty-50": (200, {})})

    def recording(method, url, timeout):
        fired.append(clock())
        return transport(method, url, timeout)

    run = drill.Drill(tmp_path, mode="overlap", transport=recording, clock=clock, run_id="d4")
    drill.run_overlap(run, ["nifty-50"], anchor=T0, tick_seconds=300, offset=2, rounds=2, sleep=clock.sleep)
    run.close()
    assert fired == [T0 + timedelta(seconds=302), T0 + timedelta(seconds=602)]


@pytest.mark.parametrize("index_id", ["banknifty", "nifty-50/../orders", "NIFTY"])
def test_unknown_indices_are_refused_before_any_request(tmp_path, index_id) -> None:
    calls = []
    run = drill.Drill(tmp_path, mode="repeat", transport=transport_returning({}, calls), clock=FakeClock())
    with pytest.raises(ValueError):
        run.fire(0, ["nifty-50", index_id])
    assert calls == []


# ---------------- launcher logging ----------------


def test_the_launcher_logs_info_with_timestamps_and_redacts(tmp_path, monkeypatch) -> None:
    import logging

    from research.phase8.tools import launch

    monkeypatch.setattr(launch, "utc_now", lambda: T0)  # same file name on both calls below
    root = logging.getLogger()
    before = (root.level, list(root.handlers))
    try:
        path = launch.configure_logging(tmp_path, run_id="l1")
        logging.getLogger("algoedge.order_manager").info("Paper order filled: %s token=%s", "ENTRY_CALL", "abc123")
        for handler in root.handlers:
            handler.flush()
        text = path.read_text()
        assert "INFO algoedge.order_manager" in text and "Paper order filled: ENTRY_CALL" in text
        assert "abc123" not in text and "token=<redacted>" in text
        with pytest.raises(FileExistsError):
            launch.configure_logging(tmp_path, run_id="l1")
    finally:
        for handler in root.handlers[:]:
            if handler not in before[1]:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(before[0])
