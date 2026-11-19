"""An in-process CompactTxStreamer with a synthetic chain, for offline tests.

Blocks are made from their height, so two fake servers agree unless told
not to. Faults: `corrupt_heights` changes one output in those blocks,
`block_delay_s` slows GetBlockRange per block, `rate_limit` answers every
call with RESOURCE_EXHAUSTED, `tip_step_s` moves the tip up once per period.
"""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent import futures

import grpc

from zcash_walletsync_bench.proto import compact_formats_pb2 as cf
from zcash_walletsync_bench.proto import service_pb2 as svc
from zcash_walletsync_bench.proto import service_pb2_grpc as svc_grpc


from zcash_walletsync_bench.synthetic import bhash, make_block, txid  # noqa: E402,F401


class Fake(svc_grpc.CompactTxStreamerServicer):
    def __init__(self, tip=500_000, corrupt_heights=(), block_delay_s=0.0, rate_limit=False, tip_step_s=None,
                 mempool_txs=2, fail_rate=0.0):
        import random
        self.fail_rate = fail_rate
        self.rng = random.Random(7)
        self.tip0 = tip
        self.t0 = time.monotonic()
        self.corrupt = set(corrupt_heights)
        self.block_delay_s = block_delay_s
        self.rate_limit = rate_limit
        self.tip_step_s = tip_step_s
        self.mempool_txs = mempool_txs
        self.calls: dict[str, int] = {}
        self.lock = threading.Lock()

    # helpers
    def tip(self) -> int:
        if self.tip_step_s:
            return self.tip0 + int((time.monotonic() - self.t0) / self.tip_step_s)
        return self.tip0

    def _count(self, name, ctx):
        with self.lock:
            self.calls[name] = self.calls.get(name, 0) + 1
        if self.rate_limit:
            ctx.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "429 Too Many Requests")
        if self.fail_rate and name not in ("GetLightdInfo", "GetLatestBlock", "GetBlock"):
            with self.lock:
                fail = self.rng.random() < self.fail_rate
            if fail:
                ctx.abort(grpc.StatusCode.UNAVAILABLE, "injected failure")

    # RPCs
    def GetLightdInfo(self, req, ctx):
        self._count("GetLightdInfo", ctx)
        return svc.LightdInfo(vendor="fake", version="0", chainName="main", blockHeight=self.tip(),
                              estimatedHeight=self.tip(), zcashdSubversion="/Fake:0/")

    def GetLatestBlock(self, req, ctx):
        self._count("GetLatestBlock", ctx)
        t = self.tip()
        return svc.BlockID(height=t, hash=bhash(t))

    def GetBlock(self, req, ctx):
        self._count("GetBlock", ctx)
        return make_block(req.height, req.height in self.corrupt)

    def GetBlockRange(self, req, ctx):
        self._count("GetBlockRange", ctx)
        lo, hi = req.start.height, req.end.height
        if hi > self.tip():
            ctx.abort(grpc.StatusCode.OUT_OF_RANGE, "end above tip")
        for h in range(lo, hi + 1):
            if self.block_delay_s:
                time.sleep(self.block_delay_s)
            yield make_block(h, h in self.corrupt)

    def GetTreeState(self, req, ctx):
        self._count("GetTreeState", ctx)
        h = req.height
        return svc.TreeState(network="main", height=h, hash=bhash(h)[::-1].hex(), time=1_700_000_000 + 75 * h,
                             saplingTree="00" * 4, orchardTree="01" * 4, ironwoodTree="02" * 4)

    def GetSubtreeRoots(self, req, ctx):
        self._count("GetSubtreeRoots", ctx)
        total = {0: 1000, 1: 700, 2: 20}[req.shieldedProtocol]
        for i in range(req.startIndex, total):
            yield svc.SubtreeRoot(rootHash=hashlib.sha256(b"r%d.%d" % (req.shieldedProtocol, i)).digest(),
                                  completingBlockHash=bhash(400_000 + i), completingBlockHeight=400_000 + i)

    def GetTransaction(self, req, ctx):
        self._count("GetTransaction", ctx)
        return svc.RawTransaction(data=b"raw" + req.hash, height=int.from_bytes(req.hash[:2], "big"))

    def GetTaddressTxids(self, req, ctx):
        self._count("GetTaddressTxids", ctx)
        if req.address.endswith("1"):
            yield svc.RawTransaction(data=b"t" + req.address.encode(), height=req.range.start.height)

    def GetAddressUtxosStream(self, req, ctx):
        self._count("GetAddressUtxosStream", ctx)
        return iter(())

    def GetMempoolStream(self, req, ctx):
        self._count("GetMempoolStream", ctx)
        for i in range(self.mempool_txs):
            yield svc.RawTransaction(data=b"mempool%d" % i, height=0)
        # lightwalletd ends the stream when a block arrives
        end = time.monotonic() + (self.tip_step_s or 1.0)
        while time.monotonic() < end and ctx.is_active():
            time.sleep(0.05)


def start(**kw):
    """Start a fake server. Returns (endpoint, fake, server)."""
    fake = Fake(**kw)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=128))
    svc_grpc.add_CompactTxStreamerServicer_to_server(fake, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    return "plain://127.0.0.1:%d" % port, fake, server


def make_fixtures(tip=500_000) -> dict:
    from zcash_walletsync_bench.fixtures import unused_t1
    return {
        "network": "main",
        "first_sync": {"tip": tip - 100, "default_blocks": 3000},
        "txids": [{"txid": txid(tip - 200, i)[::-1].hex(), "height": int.from_bytes(txid(tip - 200, i)[:2], "big"),
                   "index": i, "pools": "I"} for i in range(1, 9)],
        "taddrs_active": [unused_t1("active-%d" % i) for i in range(10)],
        "taddrs_unused": [unused_t1(str(i)) for i in range(5)],
        "transparent_range": [tip - 20_000, tip - 100],
    }
