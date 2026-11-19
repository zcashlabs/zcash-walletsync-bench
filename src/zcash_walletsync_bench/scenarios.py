"""Scenarios. Each one models one thing a wallet does; see README for sources.

A scenario function takes a `Ctx` for one service and returns a dict of
scenario-level results. The RPC samples go to the shared recorder, and the
correctness digests go to `ctx.digests`.
"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .client import Client, Recorder, probe_connection
from .digest import block_digests, parse_block, raw_tx_digest, stream_set_digest
from .fixtures import unused_t1
from .proto import service_pb2 as svc
from .wallet import PROFILES, sync_range

BLOCKS_PER_HOUR = 48  # 75 s target spacing


@dataclass
class Ctx:
    service: object  # services.Service
    recorder: Recorder
    fixtures: dict
    opts: object  # argparse namespace
    scenario: str
    common_tip: int
    digests: dict = field(default_factory=dict)
    all_services: list = field(default_factory=list)
    observer: object = None
    step: str | None = None
    rep: int | None = None
    round_: int | None = None

    def prep_client(self) -> Client:
        """A side connection whose calls are not measured (for set-up reads)."""
        return Client(self.service.name, self.service.endpoint, Recorder(), scenario="prep")

    def probe(self, samples: int) -> dict:
        return probe_connection(self.service.endpoint, samples=samples)

    def client(self, tag: str | None = None) -> Client:
        c = Client(self.service.name, self.service.endpoint, self.recorder, scenario=self.scenario, tag=tag,
                   step=self.step, rep=self.rep, round_=self.round_)
        if c.connect_error:
            raise ConnectionError(c.connect_error)
        return c


def parse_span(text: str) -> int:
    """'1w', '1d', '6h', '30m' or a block count -> blocks."""
    text = str(text).strip().lower()
    if text.endswith("w"):
        return int(float(text[:-1]) * 7 * 24 * BLOCKS_PER_HOUR)
    if text.endswith("d"):
        return int(float(text[:-1]) * 24 * BLOCKS_PER_HOUR)
    if text.endswith("h"):
        return int(float(text[:-1]) * BLOCKS_PER_HOUR)
    if text.endswith("m"):
        return max(1, int(float(text[:-1]) * BLOCKS_PER_HOUR / 60))
    return int(text)


def subtree_start_from_metadata(ctx, height: int) -> dict[int, int]:
    """Tree sizes at `height` tell a returning wallet how many subtree roots
    it already has. Read with a side connection that is not measured."""
    c = ctx.prep_client()
    try:
        raw = c.unary("GetBlock", svc.BlockID(height=height), timeout=20)
    finally:
        c.close()
    if raw is None:
        return {}
    md = parse_block(raw).chainMetadata
    # A wallet that has all completed roots asks from the last one it has.
    return {0: max(0, (md.saplingCommitmentTreeSize >> 16) - 1),
            1: max(0, (md.orchardCommitmentTreeSize >> 16) - 1),
            2: max(0, (md.ironwoodCommitmentTreeSize >> 16) - 1)}


# --------------------------------------------------------------------------
# app-open
# --------------------------------------------------------------------------

def app_open(ctx: Ctx) -> dict:
    """A wallet app starts and checks a server: new connection, GetLightdInfo,
    GetLatestBlock, then a short GetBlockRange (iOS server check: 1 block on
    launch, 100 blocks in Server Setup). Repeated with a new connection each
    time, so connection setup is measured on its own."""
    o = ctx.opts
    tip = ctx.common_tip
    probes = ctx.probe(o.app_open_samples)
    connects, first_calls = [], []
    for i in range(o.app_open_samples):
        c = ctx.client(tag="open-%d" % i)
        try:
            connects.append(c.connect_ms)
            t0 = time.perf_counter()
            c.unary("GetLightdInfo", svc.Empty(), timeout=10, tag="probe")
            first_calls.append((time.perf_counter() - t0) * 1000)
            c.unary("GetLatestBlock", svc.ChainSpec(), timeout=10, tag="probe")
            n = 1 if i % 2 == 0 else 100
            req = svc.BlockRange(start=svc.BlockID(height=tip - n + 1), end=svc.BlockID(height=tip))
            raws, ok = c.stream("GetBlockRange", req, timeout=30, tag="check-%d" % n, expect=n)
            for r in raws:
                b = parse_block(r)
                ctx.digests.setdefault("block", {})[b.height] = block_digests(b)
        finally:
            c.close()
        if o.pause_s:
            time.sleep(o.pause_s)
    r = lambda xs: sorted(x for x in xs if x is not None)  # noqa: E731
    return {
        "connection": probes,
        "grpc_connect_ms": r(connects),
        "first_rpc_ms": [round(x, 1) for x in first_calls],
        "note": "grpc_connect_ms = TCP + TLS + HTTP/2 setup as gRPC sees it; first_rpc_ms = GetLightdInfo on the new connection",
    }


# --------------------------------------------------------------------------
# first-sync and catch-up
# --------------------------------------------------------------------------

def first_sync(ctx: Ctx) -> dict:
    """Restore from a birthday: sync [birthday, fixed tip] like the chosen wallet."""
    o = ctx.opts
    fx = ctx.fixtures["first_sync"]
    tip = o.first_sync_tip or fx["tip"]
    blocks = getattr(o, "blocks", None) or fx["default_blocks"]
    start = tip - blocks + 1
    c = ctx.client(tag="w0")
    try:
        res = sync_range(c, PROFILES[o.profile], start, tip, fresh=True, active_taddrs=[],
                         max_inflight=o.max_concurrency, digests=ctx.digests)
    finally:
        c.close()
    res["connect_ms"] = c.connect_ms
    return res


def catch_up(ctx: Ctx) -> dict:
    """A returning wallet: last scanned block is `behind` blocks under the tip."""
    o = ctx.opts
    tip = o.catch_up_tip or ctx.common_tip
    results = []
    spans = [o.behind_one] if getattr(o, "behind_one", None) else o.behind
    for span in spans:
        n = parse_span(span)
        start = tip - n + 1
        sub = subtree_start_from_metadata(ctx, start - 1)
        c = ctx.client(tag="behind-%s" % span)
        try:
            res = sync_range(c, PROFILES[o.profile], start, tip, fresh=False,
                             active_taddrs=ctx.fixtures["taddrs_active"], subtree_start=sub,
                             max_inflight=o.max_concurrency, digests=ctx.digests)
        finally:
            c.close()
        res["behind"] = span
        res["subtree_start_index"] = sub
        res["connect_ms"] = c.connect_ms
        results.append(res)
        if len(spans) > 1:
            time.sleep(o.pause_s)
    return results[0] if len(results) == 1 else {"runs": results}


# --------------------------------------------------------------------------
# enhance
# --------------------------------------------------------------------------

ENHANCE_CONCURRENCY = {"vizor": 1, "librustzcash": 1, "zingo": 2}


def enhance(ctx: Ctx) -> dict:
    """After scanning finds notes, the wallet fetches each full transaction
    with GetTransaction (Vizor: one at a time; Zingo: one per scan worker,
    2 workers)."""
    o = ctx.opts
    txs = ctx.fixtures["txids"][: o.txids]
    conc = min(o.max_concurrency, ENHANCE_CONCURRENCY[o.profile])
    c = ctx.client(tag="w0")
    sizes = {}

    def one(entry):
        h = bytes.fromhex(entry["txid"])[::-1]
        raw = c.unary("GetTransaction", svc.TxFilter(hash=h), timeout=20, tag=entry["pools"])
        if raw is not None:
            d = raw_tx_digest(raw)
            if d["height"] != entry["height"]:
                d["height_mismatch"] = entry["height"]
            ctx.digests.setdefault("tx", {})[entry["txid"]] = d
            sizes[entry["txid"]] = d["size"]

    t0 = time.perf_counter()
    try:
        c.unary("GetLatestBlock", svc.ChainSpec(), timeout=10, tag="warm")
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=conc) as pool:
            list(pool.map(one, txs))
    finally:
        c.close()
    wall = time.perf_counter() - t0
    wrong_height = [t for t, d in ctx.digests.get("tx", {}).items() if "height_mismatch" in d]
    return {"profile": o.profile, "concurrency": conc, "txids": len(txs), "fetched": len(sizes),
            "bytes": sum(sizes.values()), "wall_s": round(wall, 3),
            "tx_per_s": round(len(sizes) / wall, 2) if wall > 0 else None, "wrong_height": wrong_height}


# --------------------------------------------------------------------------
# transparent
# --------------------------------------------------------------------------

def transparent(ctx: Ctx) -> dict:
    """Transparent address lookups: UTXO refresh over a set of addresses
    (Vizor, librustzcash), per-address transaction history over a range
    (Vizor: 4 streams at once; Zingo: one by one), and WebZjs' full-history
    birthday search."""
    o = ctx.opts
    fx = ctx.fixtures
    lo, hi = fx["transparent_range"]
    active = fx["taddrs_active"]
    unused = fx["taddrs_unused"] + [unused_t1("gap-%d" % i) for i in range(5)]
    addrs = active + unused  # 20 addresses, as Vizor's recent-external limit
    c = ctx.client(tag="w0")
    hist_conc = 1 if o.profile == "zingo" else min(4, o.max_concurrency)

    def history(a, start, end, step):
        req = svc.TransparentAddressBlockFilter(
            address=a, range=svc.BlockRange(start=svc.BlockID(height=start), end=svc.BlockID(height=end)))
        raws, ok = c.stream("GetTaddressTxids", req, timeout=60, tag=step)
        if ok:
            ctx.digests.setdefault("taddr_txids", {})["%s@%d-%d" % (a, start, end)] = stream_set_digest(
                raws, svc.RawTransaction)
        return len(raws) if ok else -len(raws)  # negative: the stream failed after that many

    t0 = time.perf_counter()
    try:
        c.unary("GetLatestBlock", svc.ChainSpec(), timeout=10, tag="warm")
        t0 = time.perf_counter()
        # UTXO refresh: external batch and internal batch (Vizor), 4 streams max.
        reqs = [("ext", svc.GetAddressUtxosArg(addresses=addrs, startHeight=lo)),
                ("int", svc.GetAddressUtxosArg(addresses=[unused_t1("int-%d" % i) for i in range(5)], startHeight=lo))]
        for name, r in reqs:
            raws, ok = c.stream("GetAddressUtxosStream", r, timeout=30, tag="utxo-" + name)
            if ok:
                ctx.digests.setdefault("utxos", {})[name] = stream_set_digest(raws, svc.GetAddressUtxosReply)
        t1 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=hist_conc) as pool:
            counts = list(pool.map(lambda a: history(a, lo, hi, "history"), addrs))
        failed = sum(1 for n in counts if n < 0)
        counts = [abs(n) for n in counts]
        t2 = time.perf_counter()
        # WebZjs birthday detection: one address, Sapling activation to tip
        # (webzjs-recovery/WebZjs/crates/webzjs-wallet/src/bindgen/wallet.rs:626-648).
        full = [history(a, 419200, ctx.common_tip, "full-history") for a in active[: o.full_history_addrs]]
        t3 = time.perf_counter()
    finally:
        c.close()
    return {"addresses": len(addrs), "history_concurrency": hist_conc, "range": [lo, hi],
            "txids_found": sum(counts), "history_failed": failed,
            "full_history_txids": [abs(n) for n in full], "full_history_ok": [n >= 0 for n in full],
            "utxo_s": round(t1 - t0, 3), "history_s": round(t2 - t1, 3), "full_history_s": round(t3 - t2, 3),
            "wall_s": round(t3 - t0, 3)}


# --------------------------------------------------------------------------
# mempool and tip-follow: all services at once
# --------------------------------------------------------------------------

def _mempool_loop(c: Client, stop: threading.Event, seen: dict, opens: list, deadline: float):
    """Vizor/Zingo: hold GetMempoolStream open; the server ends it when a block
    arrives; re-open at once (zingolib sync.rs:2468-2470; Vizor mempool.rs:497,652)."""
    while not stop.is_set():
        left = deadline - time.perf_counter()
        if left <= 0.5:
            break
        t0 = time.perf_counter()
        raws, ok = c.stream("GetMempoolStream", svc.Empty(), timeout=left + 30, limit_s=left, tag="mempool")
        opens.append({"at": round(t0, 3), "dur_s": round(time.perf_counter() - t0, 3), "txs": len(raws), "ok": ok})
        for raw in raws:
            t = svc.RawTransaction()
            t.ParseFromString(raw)
            seen.setdefault(_txid(t.data), time.perf_counter())
        if not ok:
            stop.wait(1.0)  # Vizor waits 1 s after an error


def _txid(data: bytes) -> str:
    # Txids of v5+ transactions are not a plain hash of the bytes, so this is
    # only a stable key for comparing mempools across services.
    import hashlib
    return hashlib.sha256(data).hexdigest()[:24]


def mempool(ctx_list: list[Ctx]) -> dict:
    """Watch the mempool on every service for the same window."""
    o = ctx_list[0].opts
    deadline = time.perf_counter() + o.duration_s
    stop = threading.Event()
    state = {}
    threads = []
    for ctx in ctx_list:
        c = ctx.client(tag="mempool")
        state[ctx.service.name] = (c, {}, [])
        th = threading.Thread(target=_mempool_loop, args=(c, stop, state[ctx.service.name][1],
                                                          state[ctx.service.name][2], deadline), daemon=True)
        threads.append(th)
        th.start()
    for th in threads:
        th.join(o.duration_s + 60)
    stop.set()
    union = set()
    for _, seen, _ in state.values():
        union |= set(seen)
    out = {}
    for name, (c, seen, opens) in state.items():
        c.close()
        out[name] = {"txs_seen": len(seen), "of_union": len(union), "stream_opens": len(opens),
                     "opens": opens[:20]}
    return out


def tip_follow(ctx_list: list[Ctx], observer) -> dict:
    """An idle wallet with the app open. Vizor's app polls GetLatestBlock every
    10 s (lib/src/providers/sync_provider.dart:1802-1845) and syncs when the tip
    moves; the sync engine holds a mempool stream open. All services run at the
    same time so the tip lag between them is measured at the same moment."""
    o = ctx_list[0].opts
    deadline = time.perf_counter() + o.duration_s
    stop = threading.Event()
    per = {}

    def wallet(ctx: Ctx):
        c = ctx.client(tag="idle")
        # Vizor keeps the mempool stream on its own connection (mempool.rs:1-25).
        mc = ctx.client(tag="idle-mempool")
        st = per[ctx.service.name] = {"client": c, "mclient": mc, "polls": 0, "new_blocks": [], "errors": 0,
                                      "mempool_seen": {}, "mempool_opens": []}
        mp = threading.Thread(target=_mempool_loop, args=(mc, stop, st["mempool_seen"], st["mempool_opens"],
                                                          deadline), daemon=True)
        mp.start()
        raw = c.unary("GetLatestBlock", svc.ChainSpec(), timeout=20, tag="poll")
        last = _height(raw) if raw else ctx.common_tip
        while not stop.is_set() and time.perf_counter() < deadline:
            if stop.wait(o.poll_s):
                break
            raw = c.unary("GetLatestBlock", svc.ChainSpec(), timeout=20, tag="poll")
            st["polls"] += 1
            if raw is None:
                st["errors"] += 1
                continue
            h = _height(raw)
            if h > last:
                seen_at = ctx.recorder.now()
                # Vizor catch-up for the new blocks: GetBlockRange with
                # GetTreeState(start-1) in parallel, then UTXO refresh.
                tsr = threading.Thread(target=c.unary, args=("GetTreeState", svc.BlockID(height=last)),
                                       kwargs={"timeout": 20, "tag": "new-block-tree"})
                tsr.start()
                req = svc.BlockRange(start=svc.BlockID(height=last + 1), end=svc.BlockID(height=h))
                raws, ok = c.stream("GetBlockRange", req, timeout=30, tag="new-block", expect=h - last)
                tsr.join()
                c.stream("GetAddressUtxosStream",
                         svc.GetAddressUtxosArg(addresses=[unused_t1("ext-%d" % i) for i in range(20)],
                                                startHeight=last + 1), timeout=20, tag="new-block-utxo")
                for r in raws:
                    b = parse_block(r)
                    ctx.digests.setdefault("block", {})[b.height] = block_digests(b)
                st["new_blocks"].append({"height": h, "seen_s": round(seen_at, 3), "blocks": len(raws), "ok": ok,
                                         "synced_s": round(ctx.recorder.now(), 3)})
                last = h
        mp.join(5)

    obs_stop = threading.Event()

    def observe():
        while not obs_stop.is_set():
            observer.snapshot("tip-follow")
            if obs_stop.wait(o.observe_s):
                break

    ot = threading.Thread(target=observe, daemon=True)
    ot.start()
    threads = [threading.Thread(target=wallet, args=(ctx,), daemon=True) for ctx in ctx_list]
    for th in threads:
        th.start()
    for th in threads:
        th.join(o.duration_s + 90)
    stop.set()
    obs_stop.set()
    ot.join(10)
    out = {}
    delays = observer.arrival_delays()
    for name, st in per.items():
        st["client"].close()
        st["mclient"].close()
        out[name] = {"polls": st["polls"], "poll_errors": st["errors"], "new_blocks": st["new_blocks"],
                     "mempool_txs_seen": len(st["mempool_seen"]), "mempool_stream_opens": len(st["mempool_opens"]),
                     "block_arrival_delay_s": delays.get(name, [])}
    return out


def _height(raw: bytes) -> int:
    b = svc.BlockID()
    b.ParseFromString(raw)
    return b.height


# --------------------------------------------------------------------------
# mixed: N wallets on one service
# --------------------------------------------------------------------------

MIX = (  # (kind, weight). Most wallets open after a short absence.
    ("catch-up-1h", 4),
    ("catch-up-1d", 3),
    ("app-open", 2),
    ("enhance", 1),
)


def mixed(ctx: Ctx) -> dict:
    """N wallets share one service at once, each on its own connection, with a
    seeded mix of behaviours and start times. The same seed gives the same
    plan on every service."""
    o = ctx.opts
    rng = random.Random(o.seed)
    kinds = [k for k, w in MIX for _ in range(w)]
    plan = [{"wallet": "wallet-%d" % i, "kind": rng.choice(kinds), "start_after_s": round(rng.uniform(0, o.stagger_s), 2)}
            for i in range(o.wallets)]
    tip = ctx.common_tip
    prof = PROFILES[o.profile]
    results = {}

    def run(w):
        if w["start_after_s"]:
            time.sleep(w["start_after_s"])
        c = ctx.client(tag=w["wallet"])
        t0 = time.perf_counter()
        try:
            if w["kind"].startswith("catch-up"):
                n = parse_span(w["kind"].split("-")[-1])
                r = sync_range(c, prof, tip - n + 1, tip, fresh=False, active_taddrs=ctx.fixtures["taddrs_active"],
                               subtree_start=sub,
                               max_inflight=max(1, o.max_concurrency // max(1, o.wallets)), digests=ctx.digests)
                ok = r["ok"]
            elif w["kind"] == "app-open":
                c.unary("GetLightdInfo", svc.Empty(), timeout=10, tag="probe")
                c.unary("GetLatestBlock", svc.ChainSpec(), timeout=10, tag="probe")
                _, ok = c.stream("GetBlockRange", svc.BlockRange(start=svc.BlockID(height=tip), end=svc.BlockID(height=tip)),
                                 timeout=30, tag="check-1", expect=1)
            else:
                ok = True
                for e in ctx.fixtures["txids"][:5]:
                    ok &= c.unary("GetTransaction", svc.TxFilter(hash=bytes.fromhex(e["txid"])[::-1]), timeout=20,
                                  tag="enhance") is not None
        finally:
            c.close()
        results[w["wallet"]] = {**w, "ok": bool(ok), "wall_s": round(time.perf_counter() - t0, 3),
                                "connect_ms": None if c.connect_ms is None else round(c.connect_ms, 1)}

    sub = subtree_start_from_metadata(ctx, tip - parse_span("1d"))
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=o.wallets) as pool:
        list(pool.map(run, plan))
    walls = sorted(r["wall_s"] for r in results.values())
    return {"wallets": o.wallets, "seed": o.seed, "plan": plan, "results": [results[w["wallet"]] for w in plan if w["wallet"] in results],
            "wall_s": round(time.perf_counter() - t0, 3),
            "wallet_wall_p50_s": walls[len(walls) // 2] if walls else None,
            "wallet_wall_max_s": walls[-1] if walls else None,
            "all_ok": all(r["ok"] for r in results.values())}


# --------------------------------------------------------------------------
# scale: the same wallet step with 1, 2, 4, 8 ... wallets at once
# --------------------------------------------------------------------------

def scale(ctx: Ctx) -> dict:
    """`o.scale_wallets` wallets start the same profile step at the same time,
    each on its own connection: a returning-wallet catch-up of
    `o.scale_behind` (default 1d) to the common tip. One level per plan step,
    so levels are repeated and interleaved like any other step."""
    o = ctx.opts
    n = o.scale_wallets
    tip = ctx.common_tip
    span = parse_span(o.scale_behind)
    start = tip - span + 1
    prof = PROFILES[o.profile]
    sub = subtree_start_from_metadata(ctx, start - 1)
    clients = [ctx.client(tag="wallet-%d" % i) for i in range(n)]
    barrier = threading.Barrier(n)
    out = [None] * n

    def one(i):
        barrier.wait()
        out[i] = sync_range(clients[i], prof, start, tip, fresh=False, active_taddrs=ctx.fixtures["taddrs_active"],
                            subtree_start=sub, max_inflight=o.max_concurrency, digests=ctx.digests)

    t0 = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=n) as pool:
            list(pool.map(one, range(n)))
    finally:
        for c in clients:
            c.close()
    wall = time.perf_counter() - t0
    done = [r for r in out if r]
    total_bytes = sum(r["bytes"] for r in done)
    total_blocks = sum(r["blocks"] for r in done)
    return {"profile": o.profile, "wallets": n, "behind": o.scale_behind, "range": [start, tip],
            "wall_s": round(wall, 3), "all_ok": all(r["ok"] for r in done) and len(done) == n,
            "aggregate_mbytes_per_s": round(total_bytes / wall / 1e6, 4) if wall > 0 else None,
            "aggregate_blocks_per_s": round(total_blocks / wall, 2) if wall > 0 else None,
            "per_wallet": [{"wall_s": r["wall_s"], "mbytes_per_s": r["mbytes_per_s"], "blocks": r["blocks"],
                            "ok": r["ok"], "time_to_first_block_s": r["time_to_first_block_s"]} for r in done]}


# --------------------------------------------------------------------------
# soak: wallet-like background traffic on every service for a long time
# --------------------------------------------------------------------------

def soak(ctx_list: list[Ctx], observer) -> dict:
    """Every service at the same time, for `o.soak_min` minutes:
    - an idle wallet polls GetLatestBlock every `o.poll_s` s (Vizor app: 10 s);
    - a mempool stream stays open and is re-opened when it ends;
    - a wallet app opens every `o.soak_app_open_s` s (new connection,
      GetLightdInfo, GetLatestBlock, 1-block GetBlockRange);
    - a returning wallet catches up 1 h every `o.soak_catch_up_s` s;
    - a wallet enhances 5 transactions every `o.soak_enhance_s` s;
    - the tip observer asks all services at once every `o.observe_s` s.
    Start offsets come from a seeded RNG. The report bins the calls by minute."""
    o = ctx_list[0].opts
    end = time.perf_counter() + o.soak_min * 60
    stop = threading.Event()
    prof = PROFILES[o.profile]
    rng = random.Random(o.seed)
    counts: dict[str, dict[str, int]] = {}
    lock = threading.Lock()

    def bump(name, kind):
        with lock:
            counts.setdefault(name, {}).setdefault(kind, 0)
            counts[name][kind] += 1

    def every(interval, offset, fn, name, kind):
        if stop.wait(offset):
            return
        while time.perf_counter() < end and not stop.is_set():
            try:
                fn()
                bump(name, kind)
            except Exception as exc:  # recorded per call; keep the soak going
                bump(name, kind + "-failed:" + type(exc).__name__)
            if stop.wait(interval):
                return

    threads = []
    for ctx in ctx_list:
        name = ctx.service.name
        idle = ctx.client(tag="soak-idle")
        mc = ctx.client(tag="soak-mempool")
        txs = ctx.fixtures["txids"]
        pos = {"tx": 0}

        def poll(c=idle):
            c.unary("GetLatestBlock", svc.ChainSpec(), timeout=20, tag="poll")

        def app(ctx=ctx):
            c = ctx.client(tag="soak-app")
            try:
                c.unary("GetLightdInfo", svc.Empty(), timeout=10, tag="probe")
                raw = c.unary("GetLatestBlock", svc.ChainSpec(), timeout=10, tag="probe")
                if raw:
                    h = _height(raw)
                    c.stream("GetBlockRange", svc.BlockRange(start=svc.BlockID(height=h), end=svc.BlockID(height=h)),
                             timeout=30, tag="check-1", expect=1)
            finally:
                c.close()

        def catch(ctx=ctx):
            c = ctx.client(tag="soak-catch-up")
            try:
                raw = c.unary("GetLatestBlock", svc.ChainSpec(), timeout=20, tag="pre-tip")
                tip = _height(raw) if raw else ctx.common_tip
                sync_range(c, prof, tip - BLOCKS_PER_HOUR + 1, tip, fresh=False,
                           active_taddrs=ctx.fixtures["taddrs_active"], max_inflight=o.max_concurrency,
                           digests=None)
            finally:
                c.close()

        def enh(ctx=ctx, pos=pos, txs=txs):
            c = ctx.client(tag="soak-enhance")
            try:
                for _ in range(5):
                    e = txs[pos["tx"] % len(txs)]
                    pos["tx"] += 1
                    c.unary("GetTransaction", svc.TxFilter(hash=bytes.fromhex(e["txid"])[::-1]), timeout=20,
                            tag="enhance")
            finally:
                c.close()

        seen, opens = {}, []
        threads.append(threading.Thread(target=_mempool_loop, args=(mc, stop, seen, opens, end), daemon=True))
        for interval, fn, kind in ((o.poll_s, poll, "poll"), (o.soak_app_open_s, app, "app-open"),
                                   (o.soak_catch_up_s, catch, "catch-up"), (o.soak_enhance_s, enh, "enhance")):
            threads.append(threading.Thread(target=every, args=(interval, rng.uniform(0, min(interval, 30)), fn,
                                                                 name, kind), daemon=True))
        ctx._soak = (idle, mc, seen, opens)

    def observe():
        while time.perf_counter() < end and not stop.is_set():
            observer.snapshot("soak")
            if stop.wait(o.observe_s):
                return

    threads.append(threading.Thread(target=observe, daemon=True))
    for th in threads:
        th.start()
    try:
        while time.perf_counter() < end:
            time.sleep(1.0)
    finally:
        stop.set()
        for th in threads:
            th.join(60)
    out = {}
    for ctx in ctx_list:
        idle, mc, seen, opens = ctx._soak
        idle.close()
        mc.close()
        out[ctx.service.name] = {"minutes": o.soak_min, "activities": counts.get(ctx.service.name, {}),
                                 "mempool_txs_seen": len(seen), "mempool_stream_opens": len(opens)}
    return out


PER_SERVICE = {
    "app-open": app_open,
    "first-sync": first_sync,
    "catch-up": catch_up,
    "enhance": enhance,
    "transparent": transparent,
    "mixed": mixed,
    "scale": scale,
}
ALL_SERVICES = {"tip-follow", "mempool", "soak"}
ORDER = ["app-open", "first-sync", "catch-up", "enhance", "transparent", "tip-follow", "mempool", "mixed", "scale",
         "soak"]
