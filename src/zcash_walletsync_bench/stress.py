"""Stress: ramp the number of concurrent wallets on our own deployments.

Each level runs `wallets` simulated wallets for `level_s` seconds. A wallet
is a thread that runs sessions one after another, with a short random think
time between them. A session is one item from the mix: a catch-up of 1 hour
or 1 day, a first-sync of a short range, an enhance of 5 transactions, or a
tip poll, each with the chosen wallet profile's real request pattern (see
wallet.py). The wallets are spread over `client_procs` worker processes
(Python threads share one core per process), each with its own gRPC
connections and its own raw files.

The parent process watches live counters from the workers. It stops a level
early, and stops the ramp, when the error rate or the p99 latency crosses a
threshold, and records why. It also samples the CPU of every core, so the
report can say whether the client or the server was the limit.

Third-party services never get more than THIRD_PARTY_MAX_WALLETS wallets.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue as queue_mod
import random
import statistics
import threading
import time
from collections import deque

from .client import H2_WINDOW, Client, Recorder
from .digest import parse_block, raw_tx_digest
from .fixtures import unused_t1  # noqa: F401  (kept for parity with scenarios)
from .proto import service_pb2 as svc
from .rawio import WORKER_FILES, RawWriter
from .services import THIRD_PARTY_MAX_WALLETS
from .wallet import PROFILES, sync_range

import itertools

DEFAULT_LEVELS = "1,2,4,8,16,32,64,128,256"
# Every level run in this process gets its own id range for calls and
# connections: seq * 1e9 + (worker + 1) * 1e7.
_LEVEL_SEQ = itertools.count(1)
DEFAULT_MIX = "catch-up-1h:40,catch-up-1d:20,first-sync:10,enhance:15,poll:15"
BLOCKS_PER_HOUR = 48


def parse_mix(text: str) -> list[tuple[str, float]]:
    out = []
    for item in str(text).split(","):
        if not item.strip():
            continue
        kind, _, w = item.partition(":")
        kind = kind.strip()
        if kind not in ("catch-up-1h", "catch-up-1d", "first-sync", "enhance", "poll"):
            raise ValueError("unknown stress mix item %r" % kind)
        out.append((kind, float(w or 1)))
    if not out:
        raise ValueError("empty stress mix")
    return out


def check_levels(service, levels: list[int]) -> list[int]:
    """Third-party services: no level above the cap, and no override."""
    if not service.own:
        bad = [n for n in levels if n > THIRD_PARTY_MAX_WALLETS]
        if bad:
            raise ValueError("stress levels %s are above the third-party cap of %d wallets for %s; "
                             "only 'local' and --own endpoints can go higher"
                             % (bad, THIRD_PARTY_MAX_WALLETS, service.name))
    return levels


# --------------------------------------------------------------------------
# Worker process
# --------------------------------------------------------------------------

class _Live:
    """Counters sent to the parent once a second."""

    def __init__(self):
        self.lock = threading.Lock()
        self._reset()

    def _reset(self):
        self.calls = self.errors = self.bytes = 0
        self.unary_ms: list[float] = []
        self.ttfb_ms: list[float] = []
        self.status: dict[str, int] = {}

    def add(self, s):
        with self.lock:
            self.calls += 1
            self.bytes += s.bytes
            if not s.ok:
                self.errors += 1
                self.status[s.status] = self.status.get(s.status, 0) + 1
            if s.method == "GetBlockRange":
                if s.ok and s.ttfb_ms is not None and len(self.ttfb_ms) < 2000:
                    self.ttfb_ms.append(s.ttfb_ms)
            elif s.method != "GetMempoolStream" and len(self.unary_ms) < 2000:
                self.unary_ms.append(s.latency_ms)

    def drain(self) -> dict:
        with self.lock:
            d = {"calls": self.calls, "errors": self.errors, "bytes": self.bytes, "unary_ms": self.unary_ms,
                 "ttfb_ms": self.ttfb_ms, "status": self.status}
            self._reset()
        return d


def _session(kind, c, cfg, rng, prof, fx, pos) -> dict:
    tip = cfg["common_tip"]
    if kind.startswith("catch-up"):
        n = BLOCKS_PER_HOUR if kind.endswith("1h") else 24 * BLOCKS_PER_HOUR
        # Returning wallets were last synced at slightly different heights.
        start = tip - n + 1 - rng.randrange(0, max(1, n // 4))
        r = sync_range(c, prof, start, tip, fresh=False, active_taddrs=fx["taddrs_active"],
                       subtree_start=cfg["subtree_start"], max_inflight=cfg["max_concurrency"], digests=None)
        return {"ok": r["ok"], "bytes": r["bytes"], "blocks": r["blocks"], "parse_s": r["client_parse_s"]}
    if kind == "first-sync":
        end = cfg["first_sync_tip"] - rng.randrange(0, 20000)
        r = sync_range(c, prof, end - cfg["first_sync_blocks"] + 1, end, fresh=True, active_taddrs=[],
                       max_inflight=cfg["max_concurrency"], digests=None)
        return {"ok": r["ok"], "bytes": r["bytes"], "blocks": r["blocks"], "parse_s": r["client_parse_s"]}
    if kind == "enhance":
        ok, nbytes, parse = True, 0, 0.0
        txs = fx["txids"]
        for _ in range(5):
            e = txs[pos["tx"] % len(txs)]
            pos["tx"] += 1
            raw = c.unary("GetTransaction", svc.TxFilter(hash=bytes.fromhex(e["txid"])[::-1]), timeout=20,
                          tag="enhance")
            if raw is None:
                ok = False
                continue
            t0 = time.perf_counter()
            raw_tx_digest(raw)
            parse += time.perf_counter() - t0
            nbytes += len(raw)
        return {"ok": ok, "bytes": nbytes, "blocks": 0, "parse_s": parse}
    raw = c.unary("GetLatestBlock", svc.ChainSpec(), timeout=20, tag="poll")
    return {"ok": raw is not None, "bytes": len(raw or b""), "blocks": 0, "parse_s": 0.0}


def worker_main(cfg: dict, stop, q) -> None:
    """One worker process: runs its wallets for one level."""
    import psutil

    H2_WINDOW["mode"] = cfg["h2_window"]
    proc = psutil.Process()
    cpu0 = proc.cpu_times()
    wall0 = time.perf_counter()
    perf_epoch = time.perf_counter() - (time.time() - cfg["run_epoch_wall"])
    sink = RawWriter(cfg["dir"], WORKER_FILES)
    live = _Live()
    rec = Recorder(sink=sink, stall_s=cfg["stall_s"], id_base=cfg["id_base"], epoch=perf_epoch,
                   keep_in_memory=False, on_call=live.add)
    prof = PROFILES[cfg["profile"]]
    fx = cfg["fixtures"]
    mix = cfg["mix"]
    kinds, weights = [k for k, _ in mix], [w for _, w in mix]
    end_at = wall0 + cfg["level_s"]
    budget = {"left": cfg["sessions"]} if cfg["sessions"] else None
    budget_lock = threading.Lock()
    clients: list = []
    clients_lock = threading.Lock()
    done = threading.Event()
    counts = {"sessions": 0, "ok": 0, "cut": 0, "parse_s": 0.0}
    counts_lock = threading.Lock()

    # Scheduling delay: how late a 20 ms sleep wakes up. With many busy
    # threads (GIL) or a loaded CPU this grows; that is client-side delay.
    delays: list[float] = []

    def ticker():
        while not done.is_set():
            t0 = time.perf_counter()
            time.sleep(0.02)
            delays.append((time.perf_counter() - t0 - 0.02) * 1000)

    def reporter():
        while not done.wait(1.0):
            q.put({"type": "live", "proc": cfg["proc"], **live.drain()})
        q.put({"type": "live", "proc": cfg["proc"], **live.drain()})

    def take_budget() -> bool:
        if budget is None:
            return True
        with budget_lock:
            if budget["left"] <= 0:
                return False
            budget["left"] -= 1
            return True

    def wallet(idx: int):
        rng = random.Random(cfg["seed"] * 1_000_003 + idx)
        pos = {"tx": rng.randrange(0, 1000)}
        c = None
        while not stop.is_set() and time.perf_counter() < end_at and take_budget():
            kind = rng.choices(kinds, weights)[0]
            if c is None or cfg["reconnect"]:
                if c is not None:
                    c.close()
                c = Client(cfg["service"], cfg["endpoint"], rec, scenario="stress", tag="w%d" % idx,
                           step=cfg["step"], rep=cfg["level"], round_=cfg["level"])
                with clients_lock:
                    clients.append(c)
                if c.connect_error:
                    sink.write("sessions", {"step": cfg["step"], "service": cfg["service"], "level": cfg["level"],
                                            "wallet": idx, "kind": "connect", "ok": False,
                                            "error": c.connect_error, "started_s": round(rec.now(), 3)})
                    c = None
                    stop.wait(1.0)
                    continue
            t0 = rec.now()
            err = None
            try:
                r = _session(kind, c, cfg, rng, prof, fx, pos)
            except Exception as exc:  # recorded; the wallet goes on
                r = {"ok": False, "bytes": 0, "blocks": 0, "parse_s": 0.0}
                err = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            t1 = rec.now()
            cut = time.perf_counter() > end_at + 0.001 or stop.is_set()
            sink.write("sessions", {"step": cfg["step"], "service": cfg["service"], "level": cfg["level"],
                                    "wallet": idx, "kind": kind, "started_s": round(t0, 3),
                                    "wall_s": round(t1 - t0, 4), "error": err, "cut": cut, **r})
            with counts_lock:
                counts["sessions"] += 1
                counts["ok"] += 1 if r["ok"] else 0
                counts["cut"] += 1 if cut else 0
                counts["parse_s"] += r["parse_s"]
            rec.forget(c.conn_id)
            think = rng.uniform(0, 2 * cfg["think_s"])
            if think and stop.wait(think):
                break
        if c is not None:
            c.close()

    threads = [threading.Thread(target=ticker, daemon=True), threading.Thread(target=reporter, daemon=True)]
    wthreads = [threading.Thread(target=wallet, args=(i,), daemon=True) for i in cfg["wallets"]]
    for t in threads + wthreads:
        t.start()
    hard_end = end_at + cfg["grace_s"]
    rss_max = 0
    while any(t.is_alive() for t in wthreads):
        rss_max = max(rss_max, proc.memory_info().rss)
        if time.perf_counter() > hard_end:
            # Past the grace time: close every connection, which cancels the
            # calls still running. Those calls are recorded as CANCELLED.
            with clients_lock:
                for c in clients:
                    c.close()
            for t in wthreads:
                t.join(5)
            break
        time.sleep(0.2)
    elapsed = time.perf_counter() - wall0
    cpu1 = proc.cpu_times()
    done.set()
    threads[1].join(3)
    d = sorted(delays)
    q.put({"type": "done", "proc": cfg["proc"], "wallets": len(cfg["wallets"]), **counts,
           "cpu_s": round((cpu1.user - cpu0.user) + (cpu1.system - cpu0.system), 3),
           "elapsed_s": round(elapsed, 3),
           "cpu_pct": round(100 * ((cpu1.user - cpu0.user) + (cpu1.system - cpu0.system)) / elapsed, 1)
           if elapsed > 0 else None,
           "rss_mb_max": round(rss_max / 2**20, 1), "threads_max": len(wthreads) + 2,
           "sched_delay_ms": {"n": len(d), "p50": round(d[len(d) // 2], 3) if d else None,
                              "p99": round(d[int(len(d) * 0.99)], 3) if d else None,
                              "max": round(d[-1], 3) if d else None}})
    sink.close()


# --------------------------------------------------------------------------
# Parent
# --------------------------------------------------------------------------

def _p99(xs):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * 0.99))]


def run_level(service, level: int, li: int, opts, fixtures, common_tip: int, subtree_start: dict, run_dir,
              run_epoch_wall: float, step_id: str, log) -> dict:
    import psutil

    nprocs = max(1, min(opts.client_procs, level, 99))
    seq = next(_LEVEL_SEQ)
    wallets = [list(range(level))[i::nprocs] for i in range(nprocs)]
    ctx = mp.get_context("spawn")
    stop = ctx.Event()
    q = ctx.Queue()
    sessions_per_proc = None
    if opts.stress_sessions:
        sessions_per_proc = [max(1, round(opts.stress_sessions * len(w) / level)) for w in wallets]
    base = {
        "service": service.name, "endpoint": service.endpoint, "level": level, "step": "%s/w%d" % (step_id, level),
        "level_s": opts.level_s, "grace_s": opts.stress_grace_s, "think_s": opts.stress_think_s,
        "profile": opts.profile.split(",")[0], "max_concurrency": opts.max_concurrency,
        "first_sync_blocks": opts.stress_first_sync_blocks,
        "first_sync_tip": opts.first_sync_tip or fixtures["first_sync"]["tip"],
        "common_tip": common_tip, "subtree_start": subtree_start, "mix": parse_mix(opts.stress_mix),
        "seed": opts.seed * 1000 + level, "fixtures": {k: fixtures[k] for k in ("txids", "taddrs_active")},
        "stall_s": opts.stall_s, "h2_window": H2_WINDOW["mode"], "reconnect": opts.stress_reconnect,
        "run_epoch_wall": run_epoch_wall,
    }
    procs = []
    for i, w in enumerate(wallets):
        cfg = dict(base, proc=i, wallets=w, sessions=sessions_per_proc[i] if sessions_per_proc else 0,
                   id_base=seq * 10**9 + (i + 1) * 10**7,
                   dir=str(run_dir / "raw" / "stress" / service.name / ("w%d" % level) / ("p%d" % i)))
        p = ctx.Process(target=worker_main, args=(cfg, stop, q), daemon=True)
        p.start()
        procs.append(p)
    t0 = time.perf_counter()
    started_s = time.time() - run_epoch_wall
    psutil.cpu_percent(percpu=True)  # start the per-core counters
    totals = {"calls": 0, "errors": 0, "bytes": 0, "status": {}}
    window: deque = deque()  # (t, unary_ms list, ttfb_ms list)
    cores, done_msgs = [], {}
    reason, stopped_at = None, None
    next_cpu = t0 + 1.0
    warm = min(10.0, opts.level_s / 3)
    deadline = t0 + opts.level_s + opts.stress_grace_s + 60
    while len(done_msgs) < nprocs and time.perf_counter() < deadline:
        try:
            m = q.get(timeout=0.25)
        except queue_mod.Empty:
            m = None
        now = time.perf_counter()
        if m and m["type"] == "done":
            done_msgs[m["proc"]] = m
        elif m and m["type"] == "live":
            totals["calls"] += m["calls"]
            totals["errors"] += m["errors"]
            totals["bytes"] += m["bytes"]
            for k, v in m["status"].items():
                totals["status"][k] = totals["status"].get(k, 0) + v
            window.append((now, m["unary_ms"], m["ttfb_ms"]))
        while window and window[0][0] < now - opts.stop_window_s:
            window.popleft()
        if now >= next_cpu:
            cores.append(psutil.cpu_percent(percpu=True))
            next_cpu = now + 1.0
        if reason is None and now - t0 >= warm and totals["calls"] >= opts.stop_min_calls:
            er = totals["errors"] / totals["calls"]
            u99 = _p99([x for _, u, _ in window for x in u])
            f99 = _p99([x for _, _, f in window for x in f])
            if er > opts.stop_error_rate:
                reason = "error rate %.3f > %.3f (%s)" % (er, opts.stop_error_rate, totals["status"])
            elif u99 is not None and u99 > opts.stop_p99_ms:
                reason = "unary p99 %.0f ms > %.0f ms over the last %.0f s" % (u99, opts.stop_p99_ms,
                                                                               opts.stop_window_s)
            elif f99 is not None and f99 > opts.stop_first_block_p99_ms:
                reason = "GetBlockRange first-block p99 %.0f ms > %.0f ms" % (f99, opts.stop_first_block_p99_ms)
            if reason:
                stopped_at = round(now - t0, 2)
                stop.set()
                log("    stopping level %d at %.1f s: %s" % (level, now - t0, reason))
    stop.set()
    for p in procs:
        p.join(30)
        if p.is_alive():
            p.terminate()
    elapsed = time.perf_counter() - t0
    active_s = min(opts.level_s, stopped_at) if stopped_at is not None else opts.level_s
    per_core = [statistics.mean(c) for c in zip(*cores)] if cores else []
    dm = list(done_msgs.values())
    client = {
        "procs": nprocs,
        "procs_reported": len(dm),
        "cpu_pct_total": round(sum(m["cpu_pct"] or 0 for m in dm), 1),
        "cpu_pct_max_proc": max((m["cpu_pct"] or 0 for m in dm), default=None),
        "rss_mb_total": round(sum(m["rss_mb_max"] for m in dm), 1),
        "sched_delay_p99_ms_max": max((m["sched_delay_ms"]["p99"] or 0 for m in dm), default=None),
        "sched_delay_p50_ms_max": max((m["sched_delay_ms"]["p50"] or 0 for m in dm), default=None),
        "parse_s": round(sum(m["parse_s"] for m in dm), 3),
        "parse_share_of_cpu": round(sum(m["parse_s"] for m in dm) / max(1e-9, sum(m["cpu_s"] for m in dm)), 4)
        if dm else None,
        "system_cpu_pct_mean": round(statistics.mean(sum(c) / len(c) for c in cores), 1) if cores else None,
        "per_core_pct_mean": [round(x, 1) for x in per_core],
        "cores_over_90pct": sum(1 for x in per_core if x > 90),
        "logical_cpus": os.cpu_count(),
        "per_proc": dm,
    }
    client["client_limited"] = bool(
        (client["cpu_pct_max_proc"] or 0) >= 90 or (client["sched_delay_p99_ms_max"] or 0) >= 100
        or (client["system_cpu_pct_mean"] or 0) >= 90)
    return {
        "level": level, "wallets": level, "level_s": opts.level_s, "active_s": round(active_s, 3),
        "started_s": round(started_s, 3), "step": "%s/w%d" % (step_id, level),
        "elapsed_s": round(elapsed, 3), "stopped_early": reason is not None, "stop_reason": reason,
        "stopped_at_s": stopped_at, "sessions": sum(m["sessions"] for m in dm),
        "sessions_ok": sum(m["ok"] for m in dm), "sessions_cut": sum(m["cut"] for m in dm),
        "live_totals": totals, "client": client, "common_tip": common_tip,
    }


def subtree_start(service, height: int) -> dict:
    c = Client(service.name, service.endpoint, Recorder(), scenario="prep")
    try:
        raw = c.unary("GetBlock", svc.BlockID(height=height), timeout=20)
    finally:
        c.close()
    if raw is None:
        return {}
    md = parse_block(raw).chainMetadata
    return {0: max(0, (md.saplingCommitmentTreeSize >> 16) - 1), 1: max(0, (md.orchardCommitmentTreeSize >> 16) - 1),
            2: max(0, (md.ironwoodCommitmentTreeSize >> 16) - 1)}


def run_ramp(service, step, opts, fixtures, observer, sink, run_dir, run_epoch_wall, log) -> list[dict]:
    levels = check_levels(service, step["overrides"]["levels"])
    out = []
    for li, level in enumerate(levels):
        snap = observer.snapshot("%s:w%d" % (step["id"], level))
        h = snap["heights"].get(service.name)
        tip = h if h is not None else max(v for v in snap["heights"].values() if v is not None)
        sub = subtree_start(service, tip - 24 * BLOCKS_PER_HOUR)
        log("  %s level %d wallets (%d s)" % (step["id"], level, opts.level_s))
        rec = run_level(service, level, li, opts, fixtures, tip, sub, run_dir, run_epoch_wall, step["id"], log)
        sink.write("runs", {"step": step["id"], "scenario": "stress", "service": service.name, "rep": li,
                            "round": None, "order_pos": None, "started_s": None, "ended_s": None,
                            "common_tip": tip, "result": rec, "error": None, "params": step["overrides"]})
        sink.write("client", {"step": step["id"], "service": service.name, "level": level, **rec["client"]})
        c = rec["client"]
        log("    sessions %d (ok %d), calls %d, errors %d, client CPU max proc %s%%, sched p99 %s ms%s" % (
            rec["sessions"], rec["sessions_ok"], rec["live_totals"]["calls"], rec["live_totals"]["errors"],
            c["cpu_pct_max_proc"], c["sched_delay_p99_ms_max"], ", CLIENT-LIMITED" if c["client_limited"] else ""))
        out.append(rec)
        if rec["stopped_early"]:
            log("  ramp stopped at %d wallets: %s" % (level, rec["stop_reason"]))
            break
        if opts.stress_cooldown_s and li + 1 < len(levels):
            time.sleep(opts.stress_cooldown_s)
    return out
