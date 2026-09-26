"""Starts the unchanged paper dashboard with INFO-level, timestamped logs
written to a campaign log file - the "logs supplied externally" of the
Phase 8.1 protocol (section 3.3). The engine's own entry point
(algoedge.web_server.main) configures no logging and runs uvicorn at
"warning", so its INFO lines (paper fills, dropped invalid bars, persistence
failures) would otherwise never be emitted.

This changes only the logging configuration of the process. Host and app
are exactly those of algoedge.web_server.main(), and so is the port unless
--port selects another; no engine code is modified or wrapped. Every log line passes through the same redaction the
evidence tools use.

    PYTHONPATH=src:. python -m research.phase8.tools.launch --log-dir research/phase8/evidence/<campaign>/logs

`--port` (default 5173, the engine's own port) selects a different loopback
port, e.g. to run a dedicated Phase 8 dashboard beside another one already
using 5173. The host is always 127.0.0.1. The database is still chosen only
by the ALGOEDGE_DB_* settings; point the other tools at the same port with
their --base-url.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from research.phase8.tools.common import IST, new_run_id, redact, utc_now

HOST = "127.0.0.1"
DEFAULT_PORT = 5173  # algoedge.web_server.main()'s port
FORMAT = "%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), limit=4000)
        record.args = ()
        return True


def configure_logging(log_dir: Path, *, run_id: str | None = None) -> Path:
    """Root logger at INFO to a new (never overwritten) file plus stderr.

    The file is claimed exactly once, with exclusive create: an existing
    file is an error, never reused. The handler itself then appends to that
    file. uvicorn.run() applies its own logging.config.dictConfig(), which
    closes every existing handler (logging.shutdown) while leaving it
    attached to the root logger; the next record makes FileHandler reopen
    its stream in the handler's mode. With mode "x" that reopen raised
    FileExistsError at the startup SYSTEM_RESTART alert; with "a" it
    continues the same file."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"server_{utc_now().astimezone(IST):%Y%m%dT%H%M%S%z}_{run_id or new_run_id()}.log"
    path.open("x", encoding="utf-8").close()  # claim a new file; FileExistsError if it already exists
    file_handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    stream_handler = logging.StreamHandler()
    formatter = logging.Formatter(FORMAT)
    formatter.default_msec_format = "%s.%03d"
    for handler in (file_handler, stream_handler):
        handler.setFormatter(formatter)
        handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    return path


def _port(text: str) -> int:
    port = int(text)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 1-65535, got {port}")
    return port


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT,
                        help=f"loopback port for the dashboard (default {DEFAULT_PORT})")
    args = parser.parse_args(argv)
    path = configure_logging(args.log_dir)
    logging.getLogger("research.phase8.launch").info("Phase 8 launcher: logging to %s; dashboard on http://%s:%d",
                                                     path, HOST, args.port)
    import uvicorn

    from algoedge.web_server import app  # imported only after logging is configured

    uvicorn.run(app, host=HOST, port=args.port, log_level="warning")  # as algoedge.web_server.main(), port selectable
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
