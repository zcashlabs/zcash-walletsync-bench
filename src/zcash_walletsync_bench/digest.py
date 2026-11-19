"""Canonical digests for cross-service correctness checks.

Two servers can encode the same data with different optional fields, so the
digests do not hash the wire bytes. They hash the fields a wallet uses:

- `block`: height, hash, prevHash, and every shielded element a wallet
  trial-decrypts or uses for note commitment trees and nullifiers (txid,
  index, Sapling spends and outputs, Orchard and Ironwood actions), plus the
  commitment tree sizes in chainMetadata. A mismatch here means a wallet
  could compute a different balance or tree. This is a hard failure.
- `aux`: fields that servers may fill differently without harm to a
  shielded wallet (time, fee, transparent vin/vout, header, transactions
  with no shielded data). A mismatch is reported as a note, not a failure.
"""

from __future__ import annotations

import hashlib

from .proto import compact_formats_pb2 as cf
from .proto import service_pb2 as svc


def _h() -> "hashlib._Hash":
    return hashlib.sha256()


def _shielded(tx: cf.CompactTx) -> bool:
    return bool(tx.spends or tx.outputs or tx.actions or tx.ironwoodActions)


def block_digests(block: cf.CompactBlock) -> dict:
    core, aux = _h(), _h()
    core.update(b"h%d|" % block.height)
    core.update(block.hash + b"|" + block.prevHash + b"|")
    md = block.chainMetadata
    core.update(b"m%d,%d,%d|" % (md.saplingCommitmentTreeSize, md.orchardCommitmentTreeSize,
                                 md.ironwoodCommitmentTreeSize))
    aux.update(b"t%d|%d|" % (block.time, len(block.header)))
    shielded_txs = 0
    for tx in block.vtx:
        if not _shielded(tx):
            aux.update(b"x" + tx.txid)
            continue
        shielded_txs += 1
        core.update(b"T%d" % tx.index + tx.txid)
        for s in tx.spends:
            core.update(b"s" + s.nf)
        for o in tx.outputs:
            core.update(b"o" + o.cmu + o.ephemeralKey + o.ciphertext)
        for a in tx.actions:
            core.update(b"a" + a.nullifier + a.cmx + a.ephemeralKey + a.ciphertext)
        for a in tx.ironwoodActions:
            core.update(b"i" + a.nullifier + a.cmx + a.ephemeralKey + a.ciphertext)
        aux.update(b"f%d" % tx.fee)
        for i in tx.vin:
            aux.update(b"v" + i.prevoutTxid + b"%d" % i.prevoutIndex)
        for o in tx.vout:
            aux.update(b"u%d" % o.value + o.scriptPubKey)
    return {
        "height": block.height,
        "hash": block.hash[::-1].hex(),
        "core": core.hexdigest()[:32],
        "aux": aux.hexdigest()[:32],
        "shielded_txs": shielded_txs,
        "txs": len(block.vtx),
    }


def parse_block(raw: bytes) -> cf.CompactBlock:
    b = cf.CompactBlock()
    b.ParseFromString(raw)
    return b


def tree_state_digest(raw: bytes) -> dict:
    t = svc.TreeState()
    t.ParseFromString(raw)
    core = _h()
    core.update(("%d|%s|%s|%s|%s" % (t.height, t.hash, t.saplingTree, t.orchardTree, t.ironwoodTree)).encode())
    return {"height": t.height, "hash": t.hash, "core": core.hexdigest()[:32], "time": t.time}


def hash_order(h: bytes) -> str:
    """A Zcash block hash has leading zero bytes in display order. Internal
    (wire) order, as CompactBlock.hash uses, puts them at the end."""
    if len(h) != 32:
        return "len%d" % len(h)
    if h[:3] == b"\0\0\0" and h[-3:] != b"\0\0\0":
        return "display"
    if h[-3:] == b"\0\0\0" and h[:3] != b"\0\0\0":
        return "internal"
    return "unknown"


def subtree_roots_digest(raws: list[bytes]) -> dict:
    """core: root hash and completing height (what wallets use, e.g.
    librustzcash sync.rs:240-255 builds CommitmentTreeRoot from these two).
    aux: completingBlockHash, which no wallet studied reads, and whose byte
    order differs between servers."""
    core, aux = _h(), _h()
    last, orders = None, set()
    for raw in raws:
        r = svc.SubtreeRoot()
        r.ParseFromString(raw)
        core.update(r.rootHash + b"%d" % r.completingBlockHeight)
        # Normalise to display order for aux, so a pure byte-order difference
        # does not hide a real one.
        h = r.completingBlockHash
        order = hash_order(h)
        orders.add(order)
        aux.update(h[::-1] if order == "internal" else h)
        last = r.completingBlockHeight
    return {"count": len(raws), "core": core.hexdigest()[:32], "aux": aux.hexdigest()[:32],
            "completing_hash_order": ",".join(sorted(orders)) or None, "last_completing_height": last}


def raw_tx_digest(raw: bytes) -> dict:
    t = svc.RawTransaction()
    t.ParseFromString(raw)
    return {"height": t.height, "size": len(t.data), "core": hashlib.sha256(t.data).hexdigest()[:32]}


def stream_set_digest(raws: list[bytes], kind) -> dict:
    """Order-independent digest of a stream of messages (e.g. txids, UTXOs)."""
    items = []
    for raw in raws:
        m = kind()
        m.ParseFromString(raw)
        items.append(m.SerializeToString(deterministic=True))
    core = _h()
    for item in sorted(items):
        core.update(hashlib.sha256(item).digest())
    return {"count": len(items), "core": core.hexdigest()[:32]}
