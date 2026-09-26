"""Phase 8 launcher logging: one new evidence log per launch, kept by every
logger across uvicorn's own logging setup (the Windows campaign-host crash:
FileExistsError at the startup SYSTEM_RESTART alert)."""

import logging
from datetime import datetime, timezone

import pytest
import uvicorn

from algoedge import alerts
from research.phase8.tools import launch

T0 = datetime(2026, 9, 26, 17, 15, 56, tzinfo=timezone.utc)  # 22:45:56 IST


@pytest.fixture()
def root_logging(monkeypatch):
    """Restores the root logger (handlers, level) after each test."""
    monkeypatch.setattr(launch, "utc_now", lambda: T0)
    root = logging.getLogger()
    before = (root.level, list(root.handlers))
    yield root
    for handler in root.handlers[:]:
        if handler not in before[1]:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(before[0])


def flush(root):
    for handler in root.handlers:
        handler.flush()


async def _asgi_app(scope, receive, send):  # never served: only uvicorn's config is exercised
    raise AssertionError("not called")


def test_startup_system_restart_alert_is_logged_after_uvicorn_configures_logging(tmp_path, root_logging, capsys):
    path = launch.configure_logging(tmp_path, run_id="bed6b6adbaf4")
    logging.getLogger("research.phase8.launch").info("Phase 8 launcher: logging to %s", path)
    # Exactly what uvicorn.run() does first: Config(...) applies dictConfig(LOGGING_CONFIG),
    # which closes every existing handler - the step that used to make the next record crash.
    uvicorn.Config(_asgi_app, host="127.0.0.1", port=5173, log_level="warning")
    alerts.raise_alert(alerts.SYSTEM_RESTART, "AlgoEdge dashboard started", source="web_server")  # as the lifespan
    logging.getLogger("algoedge.order_manager").info("Paper order filled: ENTRY_CALL 1 @ 100.00")
    flush(root_logging)

    assert [p.name for p in tmp_path.iterdir()] == [path.name]  # exactly one log file per launch
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert "Phase 8 launcher: logging to" in lines[0]  # written before uvicorn's setup ...
    assert any("WARNING algoedge.alerts" in line and "SYSTEM_RESTART" in line for line in lines)  # ... and after
    assert "INFO algoedge.order_manager" in text  # every application logger, INFO level, same file
    err = capsys.readouterr().err
    assert "SYSTEM_RESTART" in err and "Paper order filled" in err  # stderr output unchanged


def test_a_reopened_log_keeps_redacting(tmp_path, root_logging):
    path = launch.configure_logging(tmp_path, run_id="r1")
    uvicorn.Config(_asgi_app, host="127.0.0.1", port=5173, log_level="warning")
    logging.getLogger("algoedge.token_service").info("refresh failed token=%s", "abc123secret")
    flush(root_logging)
    text = path.read_text(encoding="utf-8")
    assert "abc123secret" not in text and "token=<redacted>" in text


def test_an_existing_evidence_log_is_never_overwritten_or_appended(tmp_path, root_logging):
    existing = tmp_path / "server_20260926T224556+0530_bed6b6adbaf4.log"
    existing.write_text("evidence from an earlier launch\n", encoding="utf-8")
    before = existing.stat().st_mtime_ns
    handlers_before = list(logging.getLogger().handlers)
    with pytest.raises(FileExistsError):
        launch.configure_logging(tmp_path, run_id="bed6b6adbaf4")  # same timestamp and run id
    assert existing.read_text(encoding="utf-8") == "evidence from an earlier launch\n"
    assert existing.stat().st_mtime_ns == before
    assert logging.getLogger().handlers == handlers_before  # no handler attached on failure


def test_each_launch_gets_its_own_new_file(tmp_path, root_logging):
    first = launch.configure_logging(tmp_path, run_id="a1")
    second = launch.configure_logging(tmp_path, run_id="b2")
    assert first != second and sorted(p.name for p in tmp_path.iterdir()) == sorted([first.name, second.name])


# ---------------- --port ----------------


def run_main(monkeypatch, argv):
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    assert launch.main(argv) == 0
    return calls


def test_the_default_port_is_unchanged(tmp_path, root_logging, monkeypatch):
    from algoedge import web_server

    ((app, kwargs),) = run_main(monkeypatch, ["--log-dir", str(tmp_path)])
    assert app is web_server.app
    assert kwargs == {"host": "127.0.0.1", "port": 5173, "log_level": "warning"}  # as web_server.main()


def test_the_selected_port_is_passed_to_uvicorn_on_loopback(tmp_path, root_logging, monkeypatch):
    ((_app, kwargs),) = run_main(monkeypatch, ["--log-dir", str(tmp_path), "--port", "5180"])
    assert kwargs == {"host": "127.0.0.1", "port": 5180, "log_level": "warning"}
    flush(root_logging)
    (log,) = tmp_path.iterdir()
    assert "dashboard on http://127.0.0.1:5180" in log.read_text(encoding="utf-8")  # the evidence names its port


@pytest.mark.parametrize("port", ["0", "65536", "-1", "abc"])
def test_an_invalid_port_is_rejected_before_anything_starts(tmp_path, root_logging, monkeypatch, port):
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    with pytest.raises(SystemExit):
        launch.main(["--log-dir", str(tmp_path / "logs"), "--port", port])
    assert calls == [] and not (tmp_path / "logs").exists()  # no server, no evidence log created
