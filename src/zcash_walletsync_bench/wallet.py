"""A wallet's sync traffic, without the wallet.

`sync_range` sends the same RPCs, with the same request parameters, order and
concurrency, as a real wallet's sync engine does for one range of blocks. It
does not trial-decrypt or keep a database: the benchmark measures the server.
Each `Profile` encodes one wallet. The `sources` field cites the wallet code
that each knob comes from (paths relative to ~/dev/zcash).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .client import Client, RateLimited
from .digest import block_digests, parse_block, subtree_roots_digest, tree_state_digest
from .fixtures import unused_t1
from .proto import service_pb2 as svc

POOLS = {0: "sapling", 1: "orchard", 2: "ironwood"}


@dataclass(frozen=True)
class Profile:
    name: str
    batch_blocks: int  # blocks per GetBlockRange
    inflight: int  # batches downloading at once
    tree_state: str  # "parallel": with its batch; "after": after it; "none"
    subtree_concurrent: bool
    subtree_confirm: bool  # one more GetSubtreeRoots that must come back empty
    stream_deadline_s: float
    resume_on_deadline: bool  # re-open a failed stream from the last block received
    resume_pause_s: float
    verify_blocks: int  # catch-up: first range scanned on its own
    verify_on_first_sync: bool
    transparent: str  # "utxo-batches", "utxo-one", "txids-gap"
    tip_check_after_batch: bool
    anchor_tree_state: bool  # one GetTreeState at the tip when the queue drains
    extra_tree_states: bool  # GetTreeState at birthday, birthday-1 and tip before scanning
    sources: tuple = field(default_factory=tuple)
    # The client deadlines of the real wallet, used to count how often they
    # would have fired: unary_s (whole unary call), stream_total_s (whole
    # stream), stream_start_s (to the first message), stream_msg_s (between
    # two messages).
    deadlines: dict = field(default_factory=dict)


PROFILES = {
    "vizor": Profile(
        name="vizor",
        batch_blocks=2000,
        inflight=5,
        tree_state="parallel",
        subtree_concurrent=False,
        subtree_confirm=False,
        stream_deadline_s=300.0,
        resume_on_deadline=False,
        resume_pause_s=1.0,
        verify_blocks=10,
        verify_on_first_sync=False,
        transparent="utxo-batches",
        tip_check_after_batch=True,
        anchor_tree_state=True,
        extra_tree_states=False,
        sources=(
            "sync-bench/vendor/vizor-rust/src/wallet/sync_engine/mod.rs:363 UNPLANNED_BATCH_BLOCKS=2000 above the last compiled checkpoint",
            "sync-bench/vendor/vizor-rust/src/wallet/sync_engine/mod.rs:237 PREFETCH_DEPTH=4 (4 ahead + 1 scanning = 5 in flight)",
            "sync-bench/vendor/vizor-rust/src/wallet/sync_engine/mod.rs:2797-2818 GetBlockRange and GetTreeState(start-1) joined per batch",
            "sync-bench/vendor/vizor-rust/src/wallet/sync_engine/mod.rs:769-780 GetLatestBlock after a batch when batch_end+10000 >= tip",
            "sync-bench/vendor/vizor-rust/src/wallet/sync_engine/mod.rs:263-267 20 external addresses, 4 UTXO streams at once",
            "sync-bench/vendor/vizor-rust/src/wallet/sync_engine/lwd.rs:50-53,94-98 timeouts: unary 20 s, stream idle 30 s, large downloads up to 5 min",
            "sync-bench/vendor/vizor-rust/src/wallet/sync_engine/mod.rs:1621 anchor GetTreeState at queue drain",
            "sync-bench/results/2026-11-13/zec.rocks-20k.json: 20,001 blocks -> GetBlockRange x11, GetTreeState x12, GetSubtreeRoots x3, GetAddressUtxosStream x2",
        ),
        deadlines={"unary_s": 20.0, "stream_start_s": 20.0, "stream_msg_s": 30.0,
                   "source": "sync-bench/vendor/vizor-rust/src/wallet/sync_engine/lwd.rs:50-53"},
    ),
    "librustzcash": Profile(
        name="librustzcash",
        batch_blocks=10000,
        inflight=1,
        tree_state="after",
        subtree_concurrent=False,
        subtree_confirm=False,
        stream_deadline_s=600.0,
        resume_on_deadline=False,
        resume_pause_s=0.0,
        verify_blocks=10,
        verify_on_first_sync=False,
        transparent="utxo-one",
        tip_check_after_batch=False,
        anchor_tree_state=False,
        extra_tree_states=False,
        sources=(
            "wallet-libraries/librustzcash/zcash_client_backend/src/sync.rs:60-87 run(): sequential, one request at a time",
            "wallet-libraries/librustzcash/zcash_client_backend/src/sync.rs:271-295 GetSubtreeRoots sapling, orchard, ironwood; startIndex 0, maxEntries 0",
            "wallet-libraries/librustzcash/zcash_client_backend/src/sync.rs:180-201 per chunk: GetBlockRange then GetTreeState(start-1)",
            "wallet-libraries/librustzcash/zcash_client_backend/src/sync.rs:511-528 one GetAddressUtxosStream per account",
            "wallet-libraries/librustzcash/zcash_client_sqlite/src/lib.rs:179 VERIFY_LOOKAHEAD=10",
            "webzjs-recovery/WebZjs/crates/webzjs-wallet/src/wallet.rs:60 BATCH_SIZE=10000 (WebZjs drives sync::run)",
        ),
        deadlines={"source": "wallet-libraries/librustzcash/zcash_client_backend/src/proto.rs:1140-1150 sets no timeouts"},
    ),
    "zingo": Profile(
        name="zingo",
        batch_blocks=10000,
        inflight=1,
        tree_state="none",
        subtree_concurrent=True,
        subtree_confirm=True,
        stream_deadline_s=20.0,
        resume_on_deadline=True,
        resume_pause_s=3.0,
        verify_blocks=10,
        verify_on_first_sync=True,
        transparent="txids-gap",
        tip_check_after_batch=False,
        anchor_tree_state=False,
        extra_tree_states=True,
        sources=(
            "zingolib/pepper-sync/src/client/fetch.rs:30-51 one fetcher, requests one at a time",
            "zingolib/zingo-netutils/src/time.rs:173,178 unary deadline 10 s, GetBlockRange deadline 20 s (HEAVY_UNARY_TIMEOUT)",
            "zingolib/pepper-sync/src/scan/task.rs:394-433 on DeadlineExceeded/EOF wait 3 s, re-open from the last height",
            "zingolib/pepper-sync/src/sync.rs:2244-2248 GetSubtreeRoots x3 joined, maxEntries 0; client.rs:229-238 extra empty request",
            "zingolib/pepper-sync/src/sync.rs:72 VERIFY_BLOCK_RANGE_SIZE=10",
            "zingolib/pepper-sync/src/sync/state.rs:871-896,961-986 GetTreeState at birthday-1 and tip; sync.rs:2353-2382 at birthday",
            "zingolib/pepper-sync/src/sync/state.rs:733-755 ranges cut to one shard (one stream each); state.rs:31 10,000-block ranges",
            "zingolib/pepper-sync/src/sync/transparent.rs:49-67,125-159 GetTaddressTxids per address over [last-100, tip], then gap probing",
            "zingolib/zingolib/src/config.rs:471-473 + pepper-sync/src/config.rs:165-171 default gap limit 1, external + refund scopes",
        ),
        deadlines={"unary_s": 10.0, "stream_total_s": 20.0, "stream_msg_s": 15.0,
                   "source": "zingolib/zingo-netutils/src/time.rs:170,173,178"},
    ),
}


class SyncRun:
    """State shared by the threads of one simulated sync."""

    def __init__(self, client: Client, profile: Profile, max_inflight: int, digests: dict | None):
        self.c = client
        self.p = profile
        self.inflight = max(1, min(profile.inflight, max_inflight))
        self.digests = digests if digests is not None else {}
        self.lock = threading.Lock()
        self.blocks = 0
        self.bytes = 0
        self.resumes = 0
        self.chain_breaks: list[str] = []
        self.parse_s = 0.0
        self.batch_records: list[dict] = []
        self.failed = False

    def record_blocks(self, raws: list[bytes], start: int, end: int) -> None:
        t0 = time.perf_counter()
        prev_hash, expected = None, start
        local = {}
        for raw in raws:
            b = parse_block(raw)
            if b.height != expected:
                self.chain_breaks.append("expected height %d, got %d" % (expected, b.height))
                expected = b.height
            if prev_hash is not None and b.prevHash != prev_hash:
                self.chain_breaks.append("prevHash mismatch at %d" % b.height)
            prev_hash, expected = b.hash, expected + 1
            local[b.height] = block_digests(b)
        with self.lock:
            self.digests.setdefault("block", {}).update(local)
            self.parse_s += time.perf_counter() - t0

    def get_block_range(self, start: int, end: int, tag: str) -> bool:
        """One batch. Zingo re-opens a stream that hit its deadline from the
        last block it received, as pepper-sync/src/scan/task.rs:394-433 does."""
        cur, got, ok = start, [], False
        attempts = 0
        while cur <= end:
            req = svc.BlockRange(start=svc.BlockID(height=cur), end=svc.BlockID(height=end))
            raws, ok = self.c.stream("GetBlockRange", req, timeout=self.p.stream_deadline_s,
                                     tag=tag if attempts == 0 else tag + ":resume", expect=end - cur + 1)
            got.extend(raws)
            if ok:
                break
            attempts += 1
            if not self.p.resume_on_deadline or attempts > 5 or self.c.last_error() not in (
                    "DEADLINE_EXCEEDED", "UNAVAILABLE", "SHORT_STREAM"):
                break
            with self.lock:
                self.resumes += 1
            cur = start + len(got)
            time.sleep(self.p.resume_pause_s)
        with self.lock:
            self.blocks += len(got)
            self.bytes += sum(len(r) for r in got)
        if got:
            self.record_blocks(got, start, end)
        if not ok:
            self.failed = True
        return ok

    def tree_state(self, height: int, tag: str) -> None:
        raw = self.c.unary("GetTreeState", svc.BlockID(height=height), timeout=20, tag=tag)
        if raw is not None:
            d = tree_state_digest(raw)
            with self.lock:
                self.digests.setdefault("tree_state", {})[height] = d

    def batch(self, start: int, end: int, tag: str, tip: int) -> None:
        t0 = self.c.recorder.now()
        if self.p.tree_state == "parallel":
            th = threading.Thread(target=self.tree_state, args=(start - 1, tag + ":tree"))
            th.start()
            self.get_block_range(start, end, tag)
            th.join()
        else:
            self.get_block_range(start, end, tag)
            if self.p.tree_state == "after":
                self.tree_state(start - 1, tag + ":tree")
        if self.p.tip_check_after_batch and end + 10000 >= tip:
            self.c.unary("GetLatestBlock", svc.ChainSpec(), timeout=20, tag="tip-check")
        with self.lock:
            self.batch_records.append({"start": start, "end": end, "t0_s": round(t0, 3),
                                       "t1_s": round(self.c.recorder.now(), 3)})


def split(start: int, end: int, size: int) -> list[tuple[int, int]]:
    """Inclusive [start, end] cut into ascending chunks, as sync.rs:180-197 does."""
    out, cur = [], start
    while cur <= end:
        out.append((cur, min(end, cur + size - 1)))
        cur += size
    return out


def transparent_prelude(run: SyncRun, start: int, tip: int, fresh: bool, active: list[str]) -> None:
    c, p = run.c, run.p
    if p.transparent == "utxo-batches":
        # Vizor: external and internal address batches, up to 4 streams at once.
        ext = active[:20] if not fresh else [unused_t1("ext-%d" % i) for i in range(20)]
        internal = [unused_t1("int-%d" % i) for i in range(5)]
        reqs = [svc.GetAddressUtxosArg(addresses=ext, startHeight=start, maxEntries=0),
                svc.GetAddressUtxosArg(addresses=internal, startHeight=start, maxEntries=0)]
        with ThreadPoolExecutor(max_workers=min(4, run.inflight)) as pool:
            list(pool.map(lambda r: c.stream("GetAddressUtxosStream", r, timeout=20, tag="transparent"), reqs))
    elif p.transparent == "utxo-one":
        addrs = [unused_t1("ext-%d" % i) for i in range(10)] + [unused_t1("int-%d" % i) for i in range(5)]
        if not fresh:
            addrs = active[:10] + addrs[10:]
        c.stream("GetAddressUtxosStream", svc.GetAddressUtxosArg(addresses=addrs, startHeight=start), timeout=60,
                 tag="transparent")
    elif p.transparent == "txids-gap":
        # Zingo: each known address over [last-100, tip], then one empty probe
        # per scope (gap limit 1, external + refund).
        lo = max(419200, start - 100)
        known = [] if fresh else active[:3]
        probes = [unused_t1("ext-0"), unused_t1("refund-0")]
        for a in known + probes:
            req = svc.TransparentAddressBlockFilter(
                address=a, range=svc.BlockRange(start=svc.BlockID(height=lo), end=svc.BlockID(height=tip)))
            c.stream("GetTaddressTxids", req, timeout=10, tag="transparent")


def subtree_roots(run: SyncRun, start_index: dict[int, int]) -> None:
    c, p = run.c, run.p

    def one(proto: int):
        idx = start_index.get(proto, 0)
        raws, ok = c.stream("GetSubtreeRoots",
                            svc.GetSubtreeRootsArg(startIndex=idx, shieldedProtocol=proto, maxEntries=0),
                            timeout=20, tag="subtree-" + POOLS[proto])
        if ok:
            with run.lock:
                run.digests.setdefault("subtree_roots", {})["%s@%d" % (POOLS[proto], idx)] = subtree_roots_digest(raws)
        if ok and p.subtree_confirm:
            c.stream("GetSubtreeRoots",
                     svc.GetSubtreeRootsArg(startIndex=idx + len(raws), shieldedProtocol=proto, maxEntries=0),
                     timeout=20, tag="subtree-confirm")

    if p.subtree_concurrent:
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(one, POOLS))
    else:
        for proto in POOLS:
            one(proto)


def sync_range(client: Client, profile: Profile, start: int, tip: int, *, fresh: bool,
               active_taddrs: list[str], subtree_start: dict[int, int] | None = None,
               max_inflight: int = 5, digests: dict | None = None) -> dict:
    """Simulate one sync of [start, tip] and return what happened.

    `fresh` selects first-sync behaviour (subtree roots from index 0, unused
    transparent addresses). Otherwise it is a returning wallet whose last
    scanned block is start-1.
    """
    run = SyncRun(client, profile, max_inflight, digests)
    rec = client.recorder
    t_start = rec.now()
    try:
        client.unary("GetLatestBlock", svc.ChainSpec(), timeout=20, tag="tip")
        transparent_prelude(run, start, tip, fresh, active_taddrs)
        subtree_roots(run, {} if fresh else (subtree_start or {}))
        if profile.extra_tree_states:
            for h in (start, start - 1, tip):
                run.tree_state(h, "frontier")
        ranges = []
        verify = profile.verify_blocks if (not fresh or profile.verify_on_first_sync) else 0
        if verify and profile.name == "zingo":
            # Zingo verifies the top 10 blocks first (sync/state.rs:94-99).
            v0 = max(start, tip - verify + 1)
            ranges.append((v0, tip, "verify"))
            body_end = v0 - 1
            ranges += [(a, b, "batch") for a, b in split(start, body_end, profile.batch_blocks)]
        elif verify:
            # librustzcash/Vizor: Verify = last scanned + 1 .. +10 (zcash_client_sqlite scanning.rs:841-844).
            v1 = min(tip, start + verify - 1)
            ranges.append((start, v1, "verify"))
            ranges += [(a, b, "batch") for a, b in split(v1 + 1, tip, profile.batch_blocks)]
        else:
            ranges += [(a, b, "batch") for a, b in split(start, tip, profile.batch_blocks)]
        t_first_request = rec.now()
        if ranges and ranges[0][2] == "verify":
            a, b, tag = ranges.pop(0)
            run.batch(a, b, tag, tip)
        if run.inflight == 1:
            for a, b, tag in ranges:
                run.batch(a, b, tag, tip)
                if run.failed and not profile.resume_on_deadline:
                    break
        else:
            with ThreadPoolExecutor(max_workers=run.inflight) as pool:
                list(pool.map(lambda r: run.batch(r[0], r[1], r[2], tip), ranges))
        t_blocks_done = rec.now()
        if profile.anchor_tree_state:
            run.tree_state(tip, "anchor")
    except RateLimited:
        run.failed = True
        raise
    t_end = rec.now()
    mine = [s for s in rec.select(conn_id=client.conn_id) if s.started_s >= t_start - 1e-6]
    gbr = [s for s in mine if s.method == "GetBlockRange" and s.ok and s.ttfb_ms is not None]
    first_block = min((s.started_s + s.ttfb_ms / 1000.0 for s in gbr), default=None)
    wall = t_end - t_start
    return {
        "profile": profile.name,
        "range": [start, tip],
        "blocks_expected": tip - start + 1,
        "blocks": run.blocks,
        "bytes": run.bytes,
        "ok": not run.failed and run.blocks == tip - start + 1 and not run.chain_breaks,
        "wall_s": round(wall, 3),
        "time_to_first_block_s": None if first_block is None else round(first_block - t_start, 3),
        "prelude_s": round(t_first_request - t_start, 3),
        "blocks_phase_s": round(t_blocks_done - t_first_request, 3),
        "tail_s": round(t_end - t_blocks_done, 3),
        "blocks_per_s": round(run.blocks / wall, 1) if wall > 0 else None,
        "mbytes_per_s": round(run.bytes / wall / 1e6, 3) if wall > 0 else None,
        "stream_resumes": run.resumes,
        "chain_breaks": run.chain_breaks[:10],
        "client_parse_s": round(run.parse_s, 3),
        "batches": len(run.batch_records),
        "inflight": run.inflight,
    }
