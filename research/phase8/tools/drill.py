"""Paper-only concurrency drill helper for the Phase 8.1 campaign.

The ONLY endpoint it can call is the dashboard's existing manual paper
cycle, `POST /api/auto-trading/run/{index_id}?interval=5m&quantity=1`
(a simulated fill at most - it never reaches a broker). The index must be
one of the paper engine's indices and the quantity is fixed at 1, the
scheduler's own value; nothing here can change a risk setting.

Modes (protocol drills C and D):
- overlap: at `offset` seconds after each scheduler tick (anchor + k x
  tick), fire one request per index concurrently, so manual cycles overlap
  scheduled ones;
- repeat: fire `concurrency` simultaneous requests per index, `rounds`
  times, `pause` seconds apart.

Every request is recorded - request/response timestamps, index, HTTP status
and body, a per-run id and per-request id. Nothing is assumed to succeed:
409 (CycleBusyError) and any error are kept as evidence.

    PYTHONPATH=src:. python -m research.phase8.tools.drill repeat --rounds 10 --out research/phase8/evidence/<campaign>/drills
"""

from __future__ import annotations

import argparse
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from research.phase8.tools.bars import INDEX_TICKERS
from research.phase8.tools.common import (
    Clock,
    EvidenceFile,
    Transport,
    exchange,
    new_run_id,
    urllib_transport,
    utc_now,
    validate_base_url,
)

SCHEMA = "phase8.drill.v1"
RUN_PATH = "/api/auto-trading/run/{index_id}"
FIXED_QUERY = "interval=5m&quantity=1"  # never configurable: the scheduler's own interval and quantity
DEFAULT_BASE_URL = "http://127.0.0.1:5173"


def cycle_path(index_id: str) -> str:
    if index_id not in INDEX_TICKERS:
        raise ValueError(f"unknown paper index {index_id!r}")
    return f"{RUN_PATH.format(index_id=index_id)}?{FIXED_QUERY}"


class Drill:
    def __init__(self, out_dir: Path, *, mode: str, base_url: str = DEFAULT_BASE_URL,
                 allow_non_loopback: bool = False, timeout: float = 60.0, transport: Transport = urllib_transport,
                 clock: Clock = utc_now, run_id: str | None = None) -> None:
        self.base = validate_base_url(base_url, allow_non_loopback=allow_non_loopback)
        self.mode = mode
        self.timeout = timeout
        self.transport = transport
        self.clock = clock
        self.run_id = run_id or new_run_id()
        self.evidence = EvidenceFile(out_dir, f"drill_{mode}", self.run_id, clock())
        self._lock = threading.Lock()
        self._seq = 0
        self.statuses: Counter = Counter()

    def fire(self, round_no: int, requests: Iterable[str]) -> list[dict[str, Any]]:
        """Sends the given index requests at the same moment (one thread
        each, released together) and records every result."""
        index_ids = list(requests)
        paths = [cycle_path(index_id) for index_id in index_ids]  # validate all before sending any
        barrier = threading.Barrier(len(paths))
        records: list[dict[str, Any]] = []

        def worker(index_id: str, path: str) -> None:
            barrier.wait()
            result = exchange(self.transport, "POST", self.base, path, self.timeout, self.clock)
            with self._lock:
                record = {
                    "schema": SCHEMA, "run_id": self.run_id, "mode": self.mode, "round": round_no,
                    "request_id": f"{self.run_id}-{self._seq:06d}", "index_id": index_id,
                    **result.to_record(),
                }
                self._seq += 1
                self.statuses[result.http_status if result.error is None else result.error["type"]] += 1
                records.append(self.evidence.append(record))

        threads = [threading.Thread(target=worker, args=(i, p), daemon=True) for i, p in zip(index_ids, paths)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(self.timeout + 30)
        hung = [t for t in threads if t.is_alive()]
        if hung:
            with self._lock:
                self.evidence.append({"schema": SCHEMA, "run_id": self.run_id, "mode": self.mode,
                                      "round": round_no, "hung_requests": len(hung)})
                self.statuses["HUNG"] += len(hung)
        return records

    def close(self) -> dict[str, Any]:
        summary = {"schema": SCHEMA, "run_id": self.run_id, "mode": self.mode, "summary": True,
                   "by_status": {str(k): v for k, v in sorted(self.statuses.items(), key=lambda kv: str(kv[0]))}}
        self.evidence.append(summary)
        self.evidence.close()
        return summary


def run_repeat(drill: Drill, indices: list[str], *, rounds: int, concurrency: int, pause: float,
               sleep: Callable[[float], None] = time.sleep) -> None:
    for round_no in range(rounds):
        drill.fire(round_no, [index_id for index_id in indices for _ in range(concurrency)])
        if pause > 0 and round_no < rounds - 1:
            sleep(pause)


def run_overlap(drill: Drill, indices: list[str], *, anchor: datetime, tick_seconds: float, offset: float,
                rounds: int, sleep: Callable[[float], None] = time.sleep) -> None:
    """`anchor` is an observed scheduler tick (e.g. from the server log)."""
    if anchor.tzinfo is None:
        raise ValueError("anchor must be timezone-aware")
    now = drill.clock()
    elapsed = (now - anchor).total_seconds()
    next_tick = anchor + timedelta(seconds=tick_seconds * (int(elapsed // tick_seconds) + 1))
    for round_no in range(rounds):
        due = next_tick + timedelta(seconds=tick_seconds * round_no + offset)
        wait = (due - drill.clock()).total_seconds()
        if wait > 0:
            sleep(wait)
        drill.fire(round_no, indices)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("repeat", "overlap"):
        mode = sub.add_parser(name)
        mode.add_argument("--out", type=Path, required=True)
        mode.add_argument("--indices", nargs="+", default=list(INDEX_TICKERS))
        mode.add_argument("--rounds", type=int, default=10)
        mode.add_argument("--base-url", default=DEFAULT_BASE_URL)
        mode.add_argument("--allow-non-loopback", action="store_true")
        mode.add_argument("--timeout", type=float, default=60.0)
    sub.choices["repeat"].add_argument("--concurrency", type=int, default=2)
    sub.choices["repeat"].add_argument("--pause", type=float, default=30.0)
    sub.choices["overlap"].add_argument("--anchor", required=True, help="an observed scheduler tick, ISO with offset")
    sub.choices["overlap"].add_argument("--tick-seconds", type=float, default=300.0)
    sub.choices["overlap"].add_argument("--offset", type=float, default=2.0)
    args = parser.parse_args(argv)
    for index_id in args.indices:
        cycle_path(index_id)
    drill = Drill(args.out, mode=args.mode, base_url=args.base_url, allow_non_loopback=args.allow_non_loopback,
                  timeout=args.timeout)
    try:
        if args.mode == "repeat":
            run_repeat(drill, args.indices, rounds=args.rounds, concurrency=args.concurrency, pause=args.pause)
        else:
            run_overlap(drill, args.indices, anchor=datetime.fromisoformat(args.anchor),
                        tick_seconds=args.tick_seconds, offset=args.offset, rounds=args.rounds)
    except KeyboardInterrupt:
        pass
    finally:
        summary = drill.close()
    print(drill.evidence.path, summary["by_status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
