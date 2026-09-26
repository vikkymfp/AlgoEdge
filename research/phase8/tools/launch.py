"""Starts the unchanged paper dashboard with INFO-level, timestamped logs
written to a campaign log file - the "logs supplied externally" of the
Phase 8.1 protocol (section 3.3). The engine's own entry point
(algoedge.web_server.main) configures no logging and runs uvicorn at
"warning", so its INFO lines (paper fills, dropped invalid bars, persistence
failures) would otherwise never be emitted.

This changes only the logging configuration of the process. Host, port and
app are exactly those of algoedge.web_server.main(); no engine code is
modified or wrapped. Every log line passes through the same redaction the
evidence tools use.

    PYTHONPATH=src:. python -m research.phase8.tools.launch --log-dir research/phase8/evidence/<campaign>/logs
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from research.phase8.tools.common import IST, new_run_id, redact, utc_now

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    path = configure_logging(args.log_dir)
    logging.getLogger("research.phase8.launch").info("Phase 8 launcher: logging to %s", path)
    import uvicorn

    from algoedge.web_server import app  # imported only after logging is configured

    uvicorn.run(app, host="127.0.0.1", port=5173, log_level="warning")  # as algoedge.web_server.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
