"""Compare what the services returned for the same request.

For every key (block height, tree-state height, txid, ...) that two or more
services returned, the `core` digests must be equal. When they differ, the
services are grouped by digest; a service outside the largest group is
counted as the odd one out. With two services and a tie, both are flagged.
"""

from __future__ import annotations

from collections import defaultdict

HARD = {"block": "core", "tree_state": "core", "subtree_roots": "core", "tx": "core", "taddr_txids": "core",
        "utxos": "core"}
SOFT = {"block": "aux", "subtree_roots": "aux"}
# Reported per service when it differs, e.g. completingBlockHash byte order.
NOTE_FIELDS = {"subtree_roots": "completing_hash_order"}


def compare(per_service: dict[str, dict]) -> dict:
    """per_service: {service: {kind: {key: digest dict}}}."""
    out = {"ok": True, "kinds": {}, "odd_one_out": defaultdict(int)}
    kinds = set()
    for d in per_service.values():
        kinds.update(d)
    for kind in sorted(kinds):
        keys = defaultdict(dict)
        for svc_name, d in per_service.items():
            for key, dig in d.get(kind, {}).items():
                if dig is not None:
                    keys[key][svc_name] = dig
        compared = mism = soft = 0
        examples, soft_examples = [], []
        for key, by in sorted(keys.items(), key=lambda kv: str(kv[0])):
            if len(by) < 2:
                continue
            compared += 1
            field = HARD.get(kind, "core")
            groups = defaultdict(list)
            for s, dig in by.items():
                groups[dig.get(field)].append(s)
            if len(groups) > 1:
                mism += 1
                biggest = max(len(v) for v in groups.values())
                tied = sum(1 for v in groups.values() if len(v) == biggest) > 1
                for v in groups.values():
                    if tied or len(v) < biggest:
                        for s in v:
                            out["odd_one_out"][s] += 1
                if len(examples) < 10:
                    examples.append({"key": key, "groups": {str(k): v for k, v in groups.items()},
                                     "detail": {s: dig for s, dig in by.items()}})
            sf = SOFT.get(kind)
            if sf:
                g2 = defaultdict(list)
                for s, dig in by.items():
                    g2[dig.get(sf)].append(s)
                if len(g2) > 1:
                    soft += 1
                    if len(soft_examples) < 3:
                        soft_examples.append({"key": key, "groups": {str(k): v for k, v in g2.items()}})
        out["kinds"][kind] = {"compared": compared, "mismatches": mism, "examples": examples}
        nf = NOTE_FIELDS.get(kind)
        if nf:
            seen = {}
            for svc_name, d in per_service.items():
                vals = {dig.get(nf) for dig in d.get(kind, {}).values() if dig}
                if vals:
                    seen[svc_name] = ",".join(sorted(str(v) for v in vals))
            if len(set(seen.values())) > 1:
                out["kinds"][kind][nf] = seen
        if SOFT.get(kind):
            out["kinds"][kind]["aux_differences"] = soft
            out["kinds"][kind]["aux_examples"] = soft_examples
        if mism:
            out["ok"] = False
    out["odd_one_out"] = dict(out["odd_one_out"])
    return out
