"""PNG plots from the raw data (matplotlib, Agg backend)."""

from __future__ import annotations

import pathlib
import re
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from . import rawio  # noqa: E402

COLORS = {}
PALETTE = ["#2b6cb0", "#c05621", "#2f855a", "#6b46c1", "#b83280", "#718096"]


def _color(service: str, services: list[str]) -> str:
    if service not in COLORS:
        COLORS[service] = PALETTE[services.index(service) % len(PALETTE)] if service in services else "#444"
    return COLORS[service]


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def _cdf(ax, values, label, color):
    v = np.sort(np.asarray(values, dtype=float))
    if v.size == 0:
        return
    ax.step(v, np.arange(1, v.size + 1) / v.size, where="post", label="%s (n=%d)" % (label, v.size), color=color)


def make_plots(run_dir, data: dict, summary: dict) -> dict:
    run_dir = pathlib.Path(run_dir)
    out_dir = run_dir / "plots"
    out_dir.mkdir(exist_ok=True)
    services = summary["services"]
    made = {}
    calls_by_step = defaultdict(list)
    for c in data["calls"]:
        if c.get("step"):
            calls_by_step[c["step"]].append(c)

    # 1. Latency CDF per method per service, one figure per step.
    for sid, calls in calls_by_step.items():
        counts = defaultdict(int)
        for c in calls:
            if c["ok"] and not c["cold"]:
                counts[c["method"]] += 1
        methods = [m for m, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n >= 5][:4]
        has_gbr = any(c["method"] == "GetBlockRange" and c["ok"] for c in calls)
        panels = [(m, "latency_ms") for m in methods] + ([("GetBlockRange", "ttfb_ms")] if has_gbr else [])
        if not panels:
            continue
        fig, axes = plt.subplots(1, len(panels), figsize=(4.2 * len(panels), 3.4), squeeze=False)
        for ax, (m, field) in zip(axes[0], panels):
            for s in services:
                v = [c[field] for c in calls if c["service"] == s and c["method"] == m and c["ok"]
                     and not c["cold"] and c[field] is not None]
                _cdf(ax, v, s, _color(s, services))
            ax.set_xscale("log")
            ax.set_title("%s %s" % (m, "first block" if field == "ttfb_ms" else "latency"), fontsize=9)
            ax.set_xlabel("ms (log)")
            ax.set_ylim(0, 1.01)
            ax.grid(alpha=0.3)
            ax.legend(fontsize=7)
        axes[0][0].set_ylabel("fraction of calls")
        fig.suptitle(sid, fontsize=10)
        fig.tight_layout()
        p = out_dir / ("cdf-%s.png" % _safe(sid))
        fig.savefig(p, dpi=110)
        plt.close(fig)
        made.setdefault("cdf", []).append(str(p.relative_to(run_dir)))

    # 2. Block download over time: blocks received since the repetition started, one line per
    #    repetition. (Cumulative, because a whole repetition often takes less than one second.)
    gbr = [c for c in data["calls"] if c["method"] == "GetBlockRange" and c.get("step")]
    msgs = rawio.read_messages(run_dir, {c["call_id"] for c in gbr})
    start = {}
    for c in data["calls"]:
        if c.get("step"):
            k = (c["step"], c["service"], c["rep"])
            start[k] = min(start.get(k, float("inf")), c["started_s"])
    by_rep = defaultdict(list)
    for c in gbr:
        k = (c["step"], c["service"], c["rep"])
        by_rep[k] += [c["started_s"] - start[k] + t / 1000.0 for t, _b in msgs.get(c["call_id"], [])]
    steps = sorted({k[0] for k in by_rep if summary["steps"].get(k[0], {}).get("scenario") in
                    ("first-sync", "catch-up", "scale")})
    for sid in steps:
        fig, ax = plt.subplots(figsize=(8, 3.6))
        for s in services:
            first = True
            for (st, sv, rep), ts in sorted(by_rep.items(), key=lambda kv: str(kv[0])):
                if st != sid or sv != s or not ts:
                    continue
                ts = np.sort(np.asarray(ts))
                ax.plot(np.concatenate([[0, ts[0]], ts]), np.concatenate([[0, 0], np.arange(1, ts.size + 1)]),
                        color=_color(s, services), alpha=0.6, lw=1.0, label=s if first else None)
                first = False
        ax.set_xlabel("seconds since the repetition started")
        ax.set_ylabel("blocks received")
        ax.set_title("Block download, every repetition: %s" % sid, fontsize=10)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
        fig.tight_layout()
        p = out_dir / ("throughput-%s.png" % _safe(sid))
        fig.savefig(p, dpi=110)
        plt.close(fig)
        made.setdefault("throughput", []).append(str(p.relative_to(run_dir)))

    # 3. Load curve.
    for prof, per in (summary.get("scale") or {}).items():
        has_err = any(e.get("errors") for lv in per.values() for e in lv.values())
        fig, axes = plt.subplots(1, 3 if has_err else 2, figsize=(13 if has_err else 9, 3.6))
        for s, levels in per.items():
            xs = sorted(int(k) for k in levels)
            col = _color(s, services)
            for ax, key, ylabel in ((axes[0], "aggregate_mbytes_per_s", "aggregate MB/s"),
                                    (axes[1], "first_block_ms", "GetBlockRange first block p50 ms")):
                ys = [levels[str(x)][key].get("p50") for x in xs]
                lo = [levels[str(x)][key].get("median_ci95", [None, None])[0] for x in xs]
                hi = [levels[str(x)][key].get("median_ci95", [None, None])[1] for x in xs]
                pts = [(x, y, a, b) for x, y, a, b in zip(xs, ys, lo, hi) if y is not None]
                if pts:
                    ax.plot([p[0] for p in pts], [p[1] for p in pts], "o-", color=col, label=s)
                    err = [(p[1] - p[2], p[3] - p[1]) for p in pts if p[2] is not None]
                    if len(err) == len(pts):
                        ax.errorbar([p[0] for p in pts], [p[1] for p in pts], yerr=np.array(err).T, fmt="none",
                                    ecolor=col, alpha=0.6, capsize=3)
                ax.set_ylabel(ylabel)
            if has_err:
                er = [levels[str(x)]["errors"] / max(1, levels[str(x)]["calls"]) for x in xs]
                axes[2].plot(xs, er, "o-", color=col, label=s)
                axes[2].set_ylabel("error rate")
        for ax in axes:
            ax.set_xlabel("wallets at once")
            ax.set_xscale("log", base=2)
            ax.grid(alpha=0.3)
            ax.legend(fontsize=8)
        fig.suptitle("Load curve (%s profile); bars: 95%% CI of the median%s" % (
            prof, "" if has_err else "; no call failed at any level"), fontsize=10)
        fig.tight_layout()
        p = out_dir / ("load-curve-%s.png" % _safe(prof))
        fig.savefig(p, dpi=110)
        plt.close(fig)
        made.setdefault("load_curve", []).append(str(p.relative_to(run_dir)))

    # 4. Tip lag over time.
    tips = data["tips"]
    if tips:
        fig, ax = plt.subplots(figsize=(8, 3.2))
        for i, s in enumerate(services):
            xs = [t["at_s"] / 60 for t in tips if t["lag_blocks"].get(s) is not None]
            ys = [t["lag_blocks"][s] + (i - 1) * 0.06 for t in tips if t["lag_blocks"].get(s) is not None]
            ax.plot(xs, ys, ".", ms=3, color=_color(s, services), label=s)
        ax.set_xlabel("minutes since the run started")
        ax.set_ylabel("blocks behind the highest")
        ax.set_title("Tip lag (points offset a little per service so they do not overlap)", fontsize=10)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
        fig.tight_layout()
        p = out_dir / "tip-lag.png"
        fig.savefig(p, dpi=110)
        plt.close(fig)
        made["tip_lag"] = [str(p.relative_to(run_dir))]

    # 5. Soak time series.
    if summary.get("soak"):
        fig, axes = plt.subplots(1, 2, figsize=(12, 3.6))
        for s, rows in summary["soak"].items():
            xs = [r["minute"] for r in rows]
            axes[0].plot(xs, [r["poll_p50_ms"] for r in rows], "o-", ms=3, color=_color(s, services), label=s + " p50")
            axes[0].plot(xs, [r["poll_p95_ms"] for r in rows], "x:", ms=3, color=_color(s, services), label=s + " p95")
            axes[1].plot(xs, [r["errors"] for r in rows], "o-", ms=3, color=_color(s, services), label=s)
        axes[0].set_ylabel("GetLatestBlock poll latency ms")
        axes[0].set_yscale("log")
        axes[1].set_ylabel("errors per minute")
        for ax in axes:
            ax.set_xlabel("minute of the soak")
            ax.grid(alpha=0.3)
            ax.legend(fontsize=7)
        fig.tight_layout()
        p = out_dir / "soak.png"
        fig.savefig(p, dpi=110)
        plt.close(fig)
        made["soak"] = [str(p.relative_to(run_dir))]
    # 6. Stress: throughput, latency, errors and the client against wallets.
    st = summary.get("stress") or {}
    if st:
        fig, axes = plt.subplots(2, 3, figsize=(15, 7.5))
        panels = [
            (axes[0][0], "requests/s", lambda e: [(e["requests_per_s"], "o-", "")]),
            (axes[0][1], "aggregate MB/s", lambda e: [(e["aggregate_mbytes_per_s"], "o-", "")]),
            (axes[0][2], "latency p99 ms (log)", lambda e: [(e["unary_ms"].get("p99"), "o-", " unary"),
                                                           (e["first_block_ms"].get("p99"), "x:", " first block")]),
            (axes[1][0], "error rate", lambda e: [(e["error_rate"] or 0, "o-", "")]),
            (axes[1][1], "client CPU %", lambda e: [((e["client"] or {}).get("cpu_pct_max_proc"), "o-",
                                                     " busiest worker"),
                                                    ((e["client"] or {}).get("system_cpu_pct_mean"), "x:",
                                                     " whole machine")]),
            (axes[1][2], "client scheduling delay p99 ms", lambda e: [((e["client"] or {}).get(
                "sched_delay_p99_ms_max"), "o-", "")]),
        ]
        for s_name, v in st.items():
            lv = v["levels"]
            xs = [e["wallets"] for e in lv]
            col = _color(s_name, services)
            for ax, ylabel, fn in panels:
                series = list(zip(*[fn(e) for e in lv])) if lv else []
                for ser in series:
                    ys = [y for y, _, _ in ser]
                    ax.plot(xs, ys, ser[0][1], color=col, label=s_name + ser[0][2])
                ax.set_ylabel(ylabel)
            k = v.get("knee") or {}
            if k.get("level"):
                for ax in (axes[0][0], axes[0][1], axes[0][2]):
                    ax.axvline(k["level"], color=col, alpha=0.35, ls="--")
        axes[0][2].set_yscale("log")
        axes[1][1].axhline(90, color="#999", ls=":", lw=1)
        for row in axes:
            for ax in row:
                ax.set_xscale("log", base=2)
                ax.set_xlabel("wallets at once")
                ax.grid(alpha=0.3)
                ax.legend(fontsize=7)
        fig.suptitle("Stress ramp (dashed vertical line: knee estimate)", fontsize=10)
        fig.tight_layout()
        p = out_dir / "stress.png"
        fig.savefig(p, dpi=110)
        plt.close(fig)
        made["stress"] = [str(p.relative_to(run_dir))]
    return made
