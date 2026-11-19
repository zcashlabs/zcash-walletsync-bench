"""Tip freshness: how far each service is behind the others at the same moment."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .client import Client, Recorder
from .proto import service_pb2 as svc


class TipObserver:
    """Warm connections to every service, used only for GetLatestBlock.

    A snapshot sends GetLatestBlock to all services at the same moment (one
    thread each) and reports each service's height lag behind the highest.
    """

    def __init__(self, services, recorder: Recorder, scenario: str = "tip-observer"):
        self.recorder = recorder
        self.clients = {s.name: Client(s.name, s.endpoint, recorder, scenario=scenario, tag="observer") for s in services}
        self.pool = ThreadPoolExecutor(max_workers=max(1, len(self.clients)))
        self.snapshots: list[dict] = []
        self.first_seen: dict[str, dict[int, float]] = {n: {} for n in self.clients}
        self._lock = threading.Lock()
        for name, c in self.clients.items():  # first call pays connection setup
            c.unary("GetLatestBlock", svc.ChainSpec(), timeout=15)

    def _one(self, name: str):
        raw = self.clients[name].unary("GetLatestBlock", svc.ChainSpec(), timeout=15)
        if raw is None:
            return name, None, None
        b = svc.BlockID()
        b.ParseFromString(raw)
        return name, b.height, b.hash[::-1].hex() if b.hash else None

    def snapshot(self, label: str = "") -> dict:
        at = self.recorder.now()
        results = list(self.pool.map(self._one, list(self.clients)))
        heights = {n: h for n, h, _ in results if h is not None}
        top = max(heights.values()) if heights else None
        snap = {
            "label": label,
            "at_s": round(at, 3),
            "heights": {n: h for n, h, _ in results},
            "hashes": {n: hh for n, _, hh in results},
            "lag_blocks": {n: (top - h if h is not None and top is not None else None) for n, h, _ in results},
        }
        # Same height but different hash: the services are on different chains.
        by_height: dict[int, set] = {}
        for n, h, hh in results:
            if h is not None and hh:
                by_height.setdefault(h, set()).add(hh)
        snap["hash_conflict"] = any(len(v) > 1 for v in by_height.values())
        if getattr(self.recorder, "sink", None) is not None:
            self.recorder.sink.write("tips", snap)
        with self._lock:
            self.snapshots.append(snap)
            for n, h in heights.items():
                self.first_seen[n].setdefault(h, at)
        return snap

    def common_tip(self) -> int:
        """The lowest tip across services that answered: every service has it."""
        snap = self.snapshot("common-tip")
        hs = [h for h in snap["heights"].values() if h is not None]
        if not hs:
            raise RuntimeError("no service answered GetLatestBlock")
        return min(hs)

    def arrival_delays(self) -> dict:
        """For each height seen by every service after the first snapshot, how
        many seconds after the first service each one reported it."""
        names = list(self.first_seen)
        if not names:
            return {}
        start_heights = {n: min(v) if v else None for n, v in self.first_seen.items()}
        floor = max((h for h in start_heights.values() if h is not None), default=None)
        out = {n: [] for n in names}
        common = set.intersection(*(set(v) for v in self.first_seen.values())) if names else set()
        for h in sorted(common):
            if floor is None or h <= floor:
                continue  # already known at the start: no arrival time
            first = min(self.first_seen[n][h] for n in names)
            for n in names:
                out[n].append(round(self.first_seen[n][h] - first, 3))
        return out

    def close(self) -> None:
        self.pool.shutdown(wait=False)
        for c in self.clients.values():
            c.close()


def summarize_snapshots(snapshots: list[dict]) -> dict:
    names = set()
    for s in snapshots:
        names.update(s["heights"])
    out = {}
    for n in sorted(names):
        lags = [s["lag_blocks"][n] for s in snapshots if s["lag_blocks"].get(n) is not None]
        out[n] = {
            "snapshots": len(snapshots),
            "answered": len(lags),
            "lag_max": max(lags) if lags else None,
            "lag_mean": round(sum(lags) / len(lags), 3) if lags else None,
            "behind_fraction": round(sum(1 for x in lags if x > 0) / len(lags), 3) if lags else None,
        }
    return out


def wait_interval(stop: threading.Event, seconds: float) -> bool:
    """Sleep up to `seconds`; return True if asked to stop."""
    return stop.wait(seconds)


def now() -> float:
    return time.perf_counter()
