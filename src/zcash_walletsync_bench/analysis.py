"""Statistics from the raw files of a run.

`build(run_dir)` reads run.json and raw/*, computes summary.json, writes
report.md and the plots. It uses nothing that is not in the run directory, so
`zcash-walletsync-bench report RUN_DIR` gives the same result later or on another machine.
"""

from __future__ import annotations

import json
import math
import pathlib
from collections import defaultdict

from . import rawio
from .client import STREAMING
from .stats import compare_services, describe

# Per-repetition metrics to compare between services: (field, lower is better).
KEY_METRICS = {
    "app-open": [("first_rpc_ms", True), ("grpc_connect_ms", True)],
    "first-sync": [("wall_s", True), ("mbytes_per_s", False), ("time_to_first_block_s", True)],
    "catch-up": [("wall_s", True), ("mbytes_per_s", False), ("time_to_first_block_s", True)],
    "enhance": [("wall_s", True), ("tx_per_s", False)],
    "transparent": [("wall_s", True), ("history_s", True), ("full_history_s", True)],
    "mixed": [("wallet_wall_s", True), ("wall_s", True)],
    "scale": [("aggregate_mbytes_per_s", False), ("wall_s", True), ("wallet_mbytes_per_s", False)],
    "tip-follow": [("block_arrival_delay_s", True)],
    "mempool": [],
    "soak": [],
}
# Lists inside a result that are samples of one metric (flattened over reps).
LIST_METRICS = {
    "app-open": {"first_rpc_ms": "first_rpc_ms", "grpc_connect_ms": "grpc_connect_ms"},
    "mixed": {"results": ("wall_s", "wallet_wall_s")},
    "scale": {"per_wallet": ("mbytes_per_s", "wallet_mbytes_per_s")},
    "tip-follow": {"block_arrival_delay_s": "block_arrival_delay_s"},
}
SKIP_FIELDS = {"range", "plan", "results", "per_wallet", "chain_breaks", "connection", "new_blocks", "opens",
               "subtree_start_index", "activities", "full_history_txids", "full_history_ok", "wrong_height"}


def load(run_dir) -> dict:
    run_dir = pathlib.Path(run_dir)
    return {
        "run": json.loads((run_dir / "run.json").read_text()),
        "calls": rawio.read_jsonl(run_dir, "calls"),
        "runs": rawio.read_jsonl(run_dir, "runs"),
        "tips": rawio.read_jsonl(run_dir, "tips"),
        "connections": rawio.read_jsonl(run_dir, "connections"),
        "sessions": rawio.read_jsonl(run_dir, "sessions"),
        "crosscheck": json.loads((run_dir / "raw" / "crosscheck.json").read_text())
        if (run_dir / "raw" / "crosscheck.json").exists() else {},
    }


def _rep_metrics(scenario: str, result: dict) -> dict[str, list[float]]:
    """Numeric fields of one repetition's result, plus flattened samples."""
    out: dict[str, list[float]] = defaultdict(list)
    if not isinstance(result, dict):
        return out
    for k, v in result.items():
        if k in SKIP_FIELDS:
            continue
        if isinstance(v, bool):
            out[k].append(1.0 if v else 0.0)
        elif isinstance(v, (int, float)):
            out[k].append(float(v))
    for field, spec in LIST_METRICS.get(scenario, {}).items():
        items = result.get(field) or []
        if isinstance(spec, tuple):
            sub, name = spec
            out[name] += [float(x[sub]) for x in items if isinstance(x, dict) and x.get(sub) is not None]
        else:
            out[spec] += [float(x) for x in items if x is not None]
    return out


def stream_stats(calls: list[dict], msgs: dict[int, list]) -> dict:
    """GetBlockRange streams: first-block time, per-stream rate, 1 s buckets, stalls."""
    ttfb, rate, overall, bucket_mb, bucket_blocks, max_gap = [], [], [], [], [], []
    stalls = stall_ms = 0
    for c in calls:
        if c["ttfb_ms"] is not None:
            ttfb.append(c["ttfb_ms"])
        if c.get("max_gap_ms") is not None:
            max_gap.append(c["max_gap_ms"])
        stalls += c.get("stalls") or 0
        stall_ms += c.get("stall_ms") or 0.0
        m = msgs.get(c["call_id"], [])
        if c["ok"] and c["latency_ms"] > 0 and c["bytes"]:
            overall.append(c["bytes"] / 1e6 / (c["latency_ms"] / 1000))
            transfer = c["latency_ms"] - (c["ttfb_ms"] or 0)
            if transfer > 50 and c["messages"] >= 10:
                rate.append(c["bytes"] / 1e6 / (transfer / 1000))
        if len(m) >= 2:
            t0 = m[0][0]
            nb = int((m[-1][0] - t0) // 1000) + 1
            if nb >= 3:
                by = [0] * nb
                bl = [0] * nb
                for t, size in m:
                    i = int((t - t0) // 1000)
                    by[i] += size
                    bl[i] += 1
                # The last bucket is partial; leave it out.
                bucket_mb += [b / 1e6 for b in by[:-1]]
                bucket_blocks += bl[:-1]
    return {
        "streams": len(calls),
        "first_message_ms": describe(ttfb),
        "transfer_mbytes_per_s": describe(rate),
        "overall_mbytes_per_s": describe(overall),
        "bucket_1s_mbytes_per_s": describe(bucket_mb, ci=False),
        "bucket_1s_blocks_per_s": describe(bucket_blocks, ci=False),
        "max_gap_ms": describe(max_gap, ci=False),
        "stalls": stalls,
        "stall_s": round(stall_ms / 1000, 3),
    }


def method_stats(calls: list[dict]) -> dict:
    out = {}
    by = defaultdict(list)
    for c in calls:
        by[c["method"]].append(c)
    for m, cs in sorted(by.items()):
        ok = [c for c in cs if c["ok"]]
        statuses = defaultdict(int)
        for c in cs:
            if not c["ok"]:
                statuses[c["status"]] += 1
        out[m] = {
            "calls": len(cs),
            "ok": len(ok),
            "errors": len(cs) - len(ok),
            "error_rate": round((len(cs) - len(ok)) / len(cs), 4) if cs else None,
            "errors_by_status": dict(statuses),
            "latency_ms": describe([c["latency_ms"] for c in ok if not c["cold"]]),
            "cold_latency_ms": describe([c["latency_ms"] for c in ok if c["cold"]], ci=False),
            "bytes": sum(c["bytes"] for c in ok),
        }
        if m in STREAMING:
            out[m]["first_message_ms"] = describe([c["ttfb_ms"] for c in ok if c["ttfb_ms"] is not None
                                                   and not c["cold"]])
    return out


def deadline_hits(calls: list[dict], profiles: dict) -> dict:
    """How often each wallet's own client deadlines would have fired."""
    out = {}
    for name, p in profiles.items():
        d = p.get("deadlines") or {}
        res = {}
        if "unary_s" in d:
            u = [c for c in calls if c["method"] not in STREAMING]
            res["unary > %gs" % d["unary_s"]] = [sum(1 for c in u if c["latency_ms"] > d["unary_s"] * 1000), len(u)]
        streams = [c for c in calls if c["method"] in STREAMING and c["method"] != "GetMempoolStream"]
        if "stream_total_s" in d:
            g = [c for c in streams if c["method"] == "GetBlockRange"]
            res["GetBlockRange total > %gs" % d["stream_total_s"]] = [
                sum(1 for c in g if c["latency_ms"] > d["stream_total_s"] * 1000), len(g)]
        if "stream_start_s" in d:
            res["stream first message > %gs" % d["stream_start_s"]] = [
                sum(1 for c in streams if (c["ttfb_ms"] if c["ttfb_ms"] is not None else c["latency_ms"])
                    > d["stream_start_s"] * 1000), len(streams)]
        if "stream_msg_s" in d:
            res["gap between messages > %gs" % d["stream_msg_s"]] = [
                sum(1 for c in streams if (c.get("max_gap_ms") or 0) > d["stream_msg_s"] * 1000), len(streams)]
        if res:
            out[name] = res
    return out


def tip_stats(tips: list[dict], services: list[str]) -> dict:
    lag = defaultdict(list)
    series = defaultdict(lambda: defaultdict(list))
    for t in tips:
        for s in services:
            v = t["lag_blocks"].get(s)
            if v is not None:
                lag[s].append(v)
                series[s][int(t["at_s"] // 60)].append(v)
    out = {"per_service": {}, "per_minute": {}, "arrival_delay_s": {}}
    for s in services:
        v = lag.get(s, [])
        out["per_service"][s] = {"snapshots": len(v), "lag_blocks": describe(v, ci=False),
                                 "behind_fraction": round(sum(1 for x in v if x > 0) / len(v), 4) if v else None}
        out["per_minute"][s] = {m: round(sum(x) / len(x), 3) for m, x in sorted(series[s].items())}
    # Arrival delay: regular snapshots only (tip-follow and soak).
    reg = [t for t in tips if t.get("label") in ("tip-follow", "soak")]
    first_seen = {s: {} for s in services}
    for t in reg:
        for s in services:
            h = t["heights"].get(s)
            if h is not None:
                first_seen[s].setdefault(h, t["at_s"])
    if reg:
        start = {s: min(v) if v else None for s, v in first_seen.items()}
        floor = max((h for h in start.values() if h is not None), default=None)
        common = set.intersection(*(set(v) for v in first_seen.values())) if first_seen else set()
        delays = {s: [] for s in services}
        for h in sorted(common):
            if floor is not None and h <= floor:
                continue
            first = min(first_seen[s][h] for s in services)
            for s in services:
                delays[s].append(first_seen[s][h] - first)
        out["arrival_delay_s"] = {s: describe(v) for s, v in delays.items()}
    return out


def soak_series(calls: list[dict], services: list[str]) -> dict:
    out = {}
    for s in services:
        mins = defaultdict(list)
        for c in calls:
            if c["service"] == s:
                mins[int(c["started_s"] // 60)].append(c)
        rows = []
        t0 = min(mins) if mins else 0
        for m in sorted(mins):
            cs = mins[m]
            ok = [c["latency_ms"] for c in cs if c["ok"]]
            poll = [c["latency_ms"] for c in cs if c["ok"] and c["purpose"] == "poll"]
            d = describe(ok, ci=False)
            dp = describe(poll, ci=False)
            rows.append({"minute": m - t0, "calls": len(cs), "errors": sum(1 for c in cs if not c["ok"]),
                         "p50_ms": d.get("p50"), "p95_ms": d.get("p95"), "poll_p50_ms": dp.get("p50"),
                         "poll_p95_ms": dp.get("p95")})
        out[s] = rows
    return out


def build_summary(data: dict) -> dict:
    run = data["run"]
    services = [s["name"] for s in run["services"] if "lightd_info" in s]
    calls_by_step = defaultdict(list)
    for c in data["calls"]:
        if c.get("step"):
            calls_by_step[c["step"]].append(c)
    runs_by_step = defaultdict(list)
    for r in data["runs"]:
        runs_by_step[r["step"]].append(r)
    # Stress messages are on disk but not needed for these tables; skip them.
    gbr_ids = {c["call_id"] for c in data["calls"] if c["method"] == "GetBlockRange" and c.get("scenario") != "stress"}
    msgs = data.get("_messages") or {}
    if not msgs and data.get("_run_dir"):
        msgs = rawio.read_messages(data["_run_dir"], gbr_ids)
    conns_by_step = defaultdict(list)
    for c in data["connections"]:
        conns_by_step[c.get("step")].append(c)

    out = {"label": run["label"], "services": services, "steps": {}, "scale": {}, "soak": None}
    for step in run["steps"]:
        sid, scen = step["id"], step["scenario"]
        if scen == "stress":
            continue
        calls = calls_by_step.get(sid, [])
        runs = runs_by_step.get(sid, [])
        per = {}
        for s in services:
            sc = [c for c in calls if c["service"] == s]
            sr = [r for r in runs if r["service"] == s]
            rep_vals = defaultdict(list)
            for r in sr:
                for k, v in _rep_metrics(scen, r.get("result")).items():
                    rep_vals[k] += v
            gbr = [c for c in sc if c["method"] == "GetBlockRange"]
            entry = {
                "reps": len(sr),
                "reps_failed": sum(1 for r in sr if r.get("error")),
                "rep_errors": [r["error"] for r in sr if r.get("error")][:5],
                "per_rep": {k: [round(x, 4) for x in v] for k, v in rep_vals.items()} if len(sr) <= 50 else None,
                "rep_stats": {k: describe(v) for k, v in rep_vals.items()},
                "methods": method_stats(sc),
                "connect_ms": describe([c["connect_ms"] for c in conns_by_step.get(sid, [])
                                        if c["service"] == s and c.get("connect_ms") is not None]),
                "deadlines": deadline_hits(sc, run["profiles"]),
                "retries": sum(1 for c in sc if (c.get("purpose") or "").endswith(":resume")),
            }
            if gbr:
                entry["block_streams"] = stream_stats(gbr, msgs)
            per[s] = entry
        comp = {}
        for metric, lower in KEY_METRICS.get(scen, []):
            samples = {s: _rep_raw(runs, s, scen, metric) for s in services}
            if sum(1 for v in samples.values() if v) >= 2:
                comp[metric] = compare_services(samples, lower_is_better=lower)
        # Per-method warm latency, for methods every service answered.
        for m in sorted({c["method"] for c in calls}):
            samples = {s: [c["latency_ms"] for c in calls if c["service"] == s and c["method"] == m and c["ok"]
                           and not c["cold"]] for s in services}
            if all(len(v) >= 3 for v in samples.values()) and len(samples) >= 2:
                comp["%s latency_ms" % m] = compare_services(samples, lower_is_better=True)
        out["steps"][sid] = {"scenario": scen, "params": step["overrides"], "reps": step["reps"],
                             "per_service": per, "compare": comp, "crosscheck": data["crosscheck"].get(sid)}
        if scen == "scale":
            w = step["overrides"].get("scale_wallets")
            for s in services:
                st = per[s]
                gm = st["methods"].get("GetBlockRange", {})
                out["scale"].setdefault(step["overrides"].get("profile", "?"), {}).setdefault(s, {})[str(w)] = {
                    "aggregate_mbytes_per_s": st["rep_stats"].get("aggregate_mbytes_per_s", {"n": 0}),
                    "wallet_mbytes_per_s": st["rep_stats"].get("wallet_mbytes_per_s", {"n": 0}),
                    "wall_s": st["rep_stats"].get("wall_s", {"n": 0}),
                    "first_block_ms": gm.get("first_message_ms", {"n": 0}),
                    "calls": sum(m["calls"] for m in st["methods"].values()),
                    "errors": sum(m["errors"] for m in st["methods"].values()),
                }
        if scen == "soak":
            out["soak"] = soak_series(calls, services)
    out["tips"] = tip_stats(data["tips"], services)
    out["stress"] = stress_summary(data) if any(s["scenario"] == "stress" for s in run["steps"]) else None
    out["own"] = {s["name"]: s.get("own", False) for s in run["services"]}
    return out


def _rep_raw(runs, service, scen, metric) -> list[float]:
    vals = []
    for r in runs:
        if r["service"] == service and not r.get("error"):
            vals += _rep_metrics(scen, r.get("result")).get(metric, [])
    return vals


def build(run_dir) -> dict:
    run_dir = pathlib.Path(run_dir)
    data = load(run_dir)
    data["_run_dir"] = run_dir
    summary = build_summary(data)
    try:
        from .plots import make_plots
        summary["plots"] = make_plots(run_dir, data, summary)
    except ImportError as exc:  # matplotlib missing: the tables are still complete
        summary["plots"] = {"error": "matplotlib not installed: %s" % exc}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=_json_default))
    from .report import render
    (run_dir / "report.md").write_text(render(data["run"], summary))
    return summary


def _json_default(o):
    if isinstance(o, float) and math.isnan(o):
        return None
    return str(o)


# --------------------------------------------------------------------------
# Stress
# --------------------------------------------------------------------------

def stress_summary(data: dict) -> dict:
    run = data["run"]
    profiles = run["profiles"]
    calls_by_step = defaultdict(list)
    for c in data["calls"]:
        if c.get("scenario") == "stress":
            calls_by_step[c["step"]].append(c)
    sess_by_step = defaultdict(list)
    for s in data.get("sessions", []):
        sess_by_step[s.get("step")].append(s)
    out = {}
    for r in sorted((r for r in data["runs"] if r.get("scenario") == "stress"), key=lambda r: (r["service"], r["rep"])):
        res = r["result"] or {}
        step = res.get("step") or "%s/w%d" % (r["step"], res.get("level", 0))
        cs = calls_by_step.get(step, [])
        dur = res.get("active_s") or res.get("level_s") or 1.0
        t0 = res.get("started_s")
        in_win = [c for c in cs if t0 is None or c["started_s"] <= t0 + dur]
        sess = [x for x in sess_by_step.get(step, []) if x.get("kind") != "connect"]
        by_wallet = defaultdict(int)
        for x in sess:
            by_wallet[x["wallet"]] += x.get("bytes") or 0
        wallets = res.get("wallets", 0)
        per_wallet = [by_wallet.get(i, 0) / 1e6 / dur for i in range(wallets)]
        kinds = {}
        for k in sorted({x["kind"] for x in sess}):
            ks = [x for x in sess if x["kind"] == k]
            kinds[k] = {"n": len(ks), "ok": sum(1 for x in ks if x.get("ok")),
                        "wall_s": describe([x["wall_s"] for x in ks if x.get("ok")], ci=False)}
        ms = method_stats(cs)
        unary = [c["latency_ms"] for c in cs if c["ok"] and c["method"] not in STREAMING]
        statuses = defaultdict(int)
        for c in cs:
            if not c["ok"]:
                statuses[c["status"]] += 1
        client = dict(res.get("client") or {})
        client.pop("per_proc", None)
        entry = {
            "level": res.get("level"), "wallets": wallets, "active_s": dur, "stopped_early": res.get("stopped_early"),
            "stop_reason": res.get("stop_reason"), "sessions": len(sess),
            "sessions_ok": sum(1 for x in sess if x.get("ok") and not x.get("cut")),
            "sessions_cut": sum(1 for x in sess if x.get("cut")), "sessions_by_kind": kinds,
            "requests": len(cs), "requests_per_s": round(len(in_win) / dur, 2),
            "mbytes": round(sum(c["bytes"] for c in cs) / 1e6, 3),
            "aggregate_mbytes_per_s": round(sum(c["bytes"] for c in in_win) / 1e6 / dur, 4),
            "wallet_mbytes_per_s": describe(per_wallet, ci=False),
            "unary_ms": describe(unary),
            "first_block_ms": describe([c["ttfb_ms"] for c in cs if c["method"] == "GetBlockRange" and c["ok"]
                                        and c["ttfb_ms"] is not None]),
            "methods": {m: {"calls": d["calls"], "errors": d["errors"], "p50": d["latency_ms"].get("p50"),
                            "p90": d["latency_ms"].get("p90"), "p99": d["latency_ms"].get("p99")}
                        for m, d in ms.items()},
            "errors": sum(statuses.values()),
            "error_rate": round(sum(statuses.values()) / len(cs), 5) if cs else None,
            "errors_by_status": dict(statuses),
            "deadlines": deadline_hits(cs, profiles),
            "client": client,
        }
        out.setdefault(r["service"], {"levels": []})["levels"].append(entry)
    for svc_name, v in out.items():
        v["knee"] = knee(v["levels"])
    return out


def knee(levels: list[dict]) -> dict:
    """The last level before throughput stops rising (requests/s up less
    than 10%) or latency jumps (unary p99 more than doubles and is above
    200 ms) or errors start (error rate above 1%)."""
    prev = None
    for e in levels:
        if prev is not None:
            why = None
            r0, r1 = prev["requests_per_s"], e["requests_per_s"]
            if r0 and r1 < 1.10 * r0:
                why = "requests/s rose %.0f%% from %d to %d wallets" % (100 * (r1 / r0 - 1), prev["wallets"],
                                                                       e["wallets"])
            p0, p1 = prev["unary_ms"].get("p99"), e["unary_ms"].get("p99")
            if why is None and p0 and p1 and p1 > 2 * p0 and p1 > 200:
                why = "unary p99 rose from %.0f to %.0f ms" % (p0, p1)
            if why is None and (e["error_rate"] or 0) > 0.01 >= (prev["error_rate"] or 0):
                why = "error rate rose to %.3f" % e["error_rate"]
            if why:
                cl = bool((e.get("client") or {}).get("client_limited"))
                return {"level": prev["wallets"], "reason": why, "client_limited_at_next_level": cl,
                        "note": "the client was near its limit, so the knee may be the client's" if cl else None}
        if e.get("stopped_early"):
            return {"level": prev["wallets"] if prev else None, "reason": "threshold: %s" % e["stop_reason"],
                    "client_limited_at_next_level": bool((e.get("client") or {}).get("client_limited"))}
        prev = e
    return {"level": None, "reason": "throughput still rising at the highest level run" if levels else "no levels"}
