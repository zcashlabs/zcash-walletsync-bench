"""Raw data files of a run. Everything in the report is computed from these.

Layout of a run directory:

    run.json                 meta, options, services, plan (written at start)
    raw/calls.jsonl.gz       one line per RPC
    raw/messages.csv.gz      one row per message of every server stream
    raw/connections.jsonl.gz one line per connection (setup time, errors)
    raw/runs.jsonl.gz        one line per scenario repetition per service
    raw/tips.jsonl.gz        tip snapshots (GetLatestBlock to all services at once)
    raw/crosscheck.json      correctness comparison per step
    raw/client.jsonl.gz      stress: client CPU, memory and scheduling delay per level
    raw/stress/<svc>/w<N>/p<i>/{calls,messages,connections,sessions}
                             stress: one directory per worker process and level
    summary.json             statistics (rebuilt by `zcash-walletsync-bench report`)
    report.md, plots/*.png   human-readable output (rebuilt by `zcash-walletsync-bench report`)
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import pathlib
import threading

FILES = {
    "calls": "raw/calls.jsonl.gz",
    "messages": "raw/messages.csv.gz",
    "connections": "raw/connections.jsonl.gz",
    "runs": "raw/runs.jsonl.gz",
    "tips": "raw/tips.jsonl.gz",
    "client": "raw/client.jsonl.gz",
}
# Files a stress worker process writes, relative to its own directory.
WORKER_FILES = {
    "calls": "calls.jsonl.gz",
    "messages": "messages.csv.gz",
    "connections": "connections.jsonl.gz",
    "sessions": "sessions.jsonl.gz",
}
WORKER_GLOB = "raw/stress/*/*/*"
MESSAGE_COLUMNS = ["call_id", "seq", "t_ms", "bytes"]


class RawWriter:
    """Thread-safe append-only writers. Lines are flushed every 200 records."""

    def __init__(self, run_dir: pathlib.Path, files: dict | None = None):
        self.dir = pathlib.Path(run_dir)
        files = files or FILES
        self._lock = threading.Lock()
        self._fh = {}
        self._n = 0
        for name, rel in files.items():
            (self.dir / rel).parent.mkdir(parents=True, exist_ok=True)
            self._fh[name] = io.TextIOWrapper(gzip.open(self.dir / rel, "wb", compresslevel=6), encoding="utf-8",
                                              newline="")
        self._csv = csv.writer(self._fh["messages"])
        self._csv.writerow(MESSAGE_COLUMNS)

    def _tick(self):
        self._n += 1
        if self._n % 200 == 0:
            for fh in self._fh.values():
                fh.flush()

    def write(self, kind: str, record: dict) -> None:
        line = json.dumps(record, separators=(",", ":"), default=str)
        with self._lock:
            self._fh[kind].write(line + "\n")
            self._tick()

    def messages(self, call_id: int, rows: list[tuple[float, int]]) -> None:
        with self._lock:
            for seq, (t_ms, size) in enumerate(rows):
                self._csv.writerow((call_id, seq, "%.2f" % t_ms, size))
            self._tick()

    def close(self) -> None:
        with self._lock:
            for fh in self._fh.values():
                fh.close()


def _paths(run_dir, kind: str, stress: bool) -> list[pathlib.Path]:
    run_dir = pathlib.Path(run_dir)
    out = []
    if kind in FILES:
        out.append(run_dir / FILES[kind])
    if stress and kind in WORKER_FILES:
        out += sorted(run_dir.glob(WORKER_GLOB + "/" + WORKER_FILES[kind]))
    return [p for p in out if p.exists()]


def read_jsonl(run_dir, kind: str, stress: bool = True) -> list[dict]:
    """All records of one kind: the main file and, with stress=True, every
    stress worker's file."""
    out = []
    for p in _paths(run_dir, kind, stress):
        try:
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except json.JSONDecodeError:
                            break  # a run stopped mid-write; keep what is complete
        except (EOFError, OSError):
            pass
    return out


def read_messages(run_dir, call_ids: set[int] | None = None) -> dict[int, list[tuple[float, int]]]:
    """call_id -> [(t_ms since call start, bytes), ...]."""
    out: dict[int, list] = {}
    for p in _paths(run_dir, "messages", stress=call_ids is not None):
        try:
            with gzip.open(p, "rt", encoding="utf-8", newline="") as fh:
                r = csv.reader(fh)
                next(r, None)
                for row in r:
                    if len(row) != 4:
                        break
                    cid = int(row[0])
                    if call_ids is not None and cid not in call_ids:
                        continue
                    out.setdefault(cid, []).append((float(row[2]), int(row[3])))
        except (EOFError, OSError):
            pass  # truncated tail of an interrupted run
    return out
