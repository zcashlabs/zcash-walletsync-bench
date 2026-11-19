"""A synthetic chain made from block heights. Used by the dry-run planner and
by the offline test server. Two calls with the same height give the same block."""

from __future__ import annotations

import hashlib

from .proto import compact_formats_pb2 as cf


def bhash(h: int) -> bytes:
    return hashlib.sha256(b"block%d" % h).digest()


def txid(h: int, i: int) -> bytes:
    return hashlib.sha256(b"tx%d.%d" % (h, i)).digest()


def make_block(h: int, corrupt: bool = False) -> cf.CompactBlock:
    b = cf.CompactBlock(height=h, hash=bhash(h), prevHash=bhash(h - 1), time=1_700_000_000 + 75 * h)
    b.chainMetadata.saplingCommitmentTreeSize = 70_000_000 + h
    b.chainMetadata.orchardCommitmentTreeSize = 50_000_000 + 2 * h
    b.chainMetadata.ironwoodCommitmentTreeSize = 1_000_000 + 3 * h
    for i in range(1, 3):
        tx = b.vtx.add(index=i, txid=txid(h, i))
        seed = hashlib.sha256(b"o%d.%d" % (h, i)).digest()
        tx.outputs.add(cmu=seed, ephemeralKey=seed, ciphertext=(seed * 2)[:52])
        a = tx.ironwoodActions.add(nullifier=seed, cmx=seed, ephemeralKey=seed, ciphertext=(seed * 2)[:52])
        if corrupt and i == 1:
            a.cmx = b"\x00" * 32
    return b
