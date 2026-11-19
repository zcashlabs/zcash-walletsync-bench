"""Deterministic inputs taken from real mainnet data.

`build_fixtures` reads one fixed block window from one service and picks
txids and transparent addresses by fixed rules (sorted, first N), so two
people who run it on the same window get the same file. The committed file
`fixtures/mainnet.json` is what every scenario uses by default, and its
content and sha256 are copied into each result file.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from collections import Counter

from .client import Client, Recorder
from .digest import parse_block
from .proto import service_pb2 as svc

DEFAULT_PATH = pathlib.Path(__file__).resolve().parent / "data" / "mainnet.json"

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
T1_PREFIX = bytes([0x1C, 0xB8])  # mainnet P2PKH ("t1...")


def base58check(payload: bytes) -> str:
    data = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    pad = len(data) - len(data.lstrip(b"\0"))
    return "1" * pad + out


def t1_address(hash160: bytes) -> str:
    assert len(hash160) == 20
    return base58check(T1_PREFIX + hash160)


def unused_t1(label: str) -> str:
    """A valid t1 address derived from a label. No one holds its key, so it has
    no history. It stands for the empty gap-limit addresses a wallet probes."""
    return t1_address(hashlib.sha256(("lwd-wallet-bench unused " + label).encode()).digest()[:20])


def load(path: str | pathlib.Path | None = None) -> dict:
    p = pathlib.Path(path) if path else DEFAULT_PATH
    raw = p.read_bytes()
    data = json.loads(raw)
    data["_sha256"] = hashlib.sha256(raw).hexdigest()
    data["_path"] = str(p.name)
    return data


def build_fixtures(endpoint: str, start: int, end: int, first_sync_end: int, n_txids: int = 40,
                   n_taddrs: int = 10, n_unused: int = 5) -> dict:
    rec = Recorder()
    c = Client("fixtures", endpoint, rec, scenario="fixtures")
    try:
        req = svc.BlockRange(start=svc.BlockID(height=start), end=svc.BlockID(height=end),
                             poolTypes=[svc.TRANSPARENT, svc.SAPLING, svc.ORCHARD, svc.IRONWOOD])
        raws, ok = c.stream("GetBlockRange", req, timeout=300, expect=end - start + 1)
        if not ok:
            raise RuntimeError("could not read fixture window: %s" % rec.samples[-1].error)
    finally:
        c.close()
    shielded, transparent, addr_count = [], [], Counter()
    for raw in raws:
        b = parse_block(raw)
        for tx in b.vtx:
            kinds = "".join(k for k, v in (("sapling", tx.spends or tx.outputs), ("orchard", tx.actions),
                                            ("ironwood", tx.ironwoodActions), ("transparent", tx.vin or tx.vout)) if v)
            entry = {"txid": tx.txid[::-1].hex(), "height": b.height, "index": tx.index,
                     "pools": kinds.replace("sapling", "S").replace("orchard", "O").replace("ironwood", "I")
                     .replace("transparent", "T")}
            if tx.index == 0:
                continue  # coinbase: wallets rarely enhance it
            if tx.spends or tx.outputs or tx.actions or tx.ironwoodActions:
                shielded.append(entry)
            else:
                transparent.append(entry)
            for o in tx.vout:
                sp = o.scriptPubKey
                if len(sp) == 25 and sp[:3] == b"\x76\xa9\x14" and sp[23:] == b"\x88\xac":
                    addr_count[sp[3:23]] += 1
    shielded.sort(key=lambda e: (e["height"], e["index"]))
    transparent.sort(key=lambda e: (e["height"], e["index"]))
    # Mix of pools, in chain order. Most wallets enhance a few shielded txs and
    # some with transparent parts.
    def spread(items, k):  # evenly spaced, so one busy block does not fill the list
        if len(items) <= k:
            return items
        return [items[i * len(items) // k] for i in range(k)]

    txids = spread(shielded, n_txids - n_txids // 4) + spread(transparent, n_txids // 4)
    # Ordinary addresses: seen 1-3 times in the window. Busy exchange or pool
    # addresses are left out so lookups stay cheap for the servers.
    ordinary = sorted(h for h, n in addr_count.items() if 1 <= n <= 3)
    active = [t1_address(h) for h in ordinary[:n_taddrs]]
    return {
        "network": "main",
        "source": {"window": [start, end], "rule": "txids: non-coinbase shielded txs by (height, index), evenly spaced, "
                   "then transparent-only the same way; taddrs: P2PKH outputs seen 1-3 times in the window, sorted by hash160"},
        "first_sync": {"tip": first_sync_end, "default_blocks": 10000,
                       "note": "fixed 'tip' for a restore; birthday = tip - blocks + 1"},
        "txids": txids,
        "taddrs_active": active,
        "taddrs_unused": [unused_t1(str(i)) for i in range(n_unused)],
        "transparent_range": [start - 10000, end],
    }
