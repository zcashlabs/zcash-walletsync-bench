"""SVG figures of the site page (matplotlib, Agg).

The look follows the ztreamer benchmark site: dark surface, Sora titles, JetBrains Mono text,
one fixed colour per service. Every figure also writes the numbers it draws to data/<name>.csv.
"""

from __future__ import annotations

import csv
import math
import os
import pathlib
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, LogLocator, MaxNLocator, NullFormatter, NullLocator  # noqa: E402

from . import rawio  # noqa: E402

os.environ.setdefault("SOURCE_DATE_EPOCH", "1794528000")
FONTS = pathlib.Path(__file__).parent / "fonts"

# Tokens of site_assets/site.css. Keep the two in step.
BG, PANEL, RULE, AXIS = "#171415", "#211d1e", "#363132", "#4a4445"
INK, DIM, FAINT = "#f4f4f4", "#a3a3a3", "#6f6a6b"
# Categorical slots, validated in this order on BG (CVD, normal vision, 3:1 contrast).
SERIES = ["#3987e5", "#d95926", "#199e70", "#9085e9"]
# A service keeps its colour across runs; unknown names take the free slots in order.
FIXED_SLOT = {"local": 0, "ours-fra": 0, "ours": 3, "ours-sgp": 3, "zcashlabs": 1, "zecrocks": 2}
# Neutral ramp for ordered categories inside one bar (lag 0, 1, 2, 3+).
RAMP = ["#4a4445", "#7d7677", "#b3adae", "#f4f4f4"]

_fonts_done = False


def setup():
    global _fonts_done
    if not _fonts_done:
        for f in ("JetBrainsMono-Regular.ttf", "Sora-Bold.ttf"):
            if (FONTS / f).exists():
                font_manager.fontManager.addfont(str(FONTS / f))
        _fonts_done = True
    plt.rcParams.update({
        "font.family": ["JetBrains Mono", "DejaVu Sans Mono"], "svg.hashsalt": "zcash-walletsync-bench", "font.size": 10,
        "axes.titlesize": 11, "axes.titlepad": 10, "axes.titlelocation": "left", "text.color": INK,
        "axes.labelcolor": DIM, "xtick.color": DIM, "ytick.color": DIM, "axes.edgecolor": AXIS,
        "figure.facecolor": BG, "axes.facecolor": BG, "savefig.facecolor": BG, "grid.color": INK,
        "grid.alpha": .07, "legend.facecolor": BG, "legend.edgecolor": RULE, "svg.fonttype": "path",
        "axes.spines.top": False, "axes.spines.right": False,
    })


def colors(services: list[str]) -> dict[str, str]:
    out, used = {}, set()
    for s in services:
        if s in FIXED_SLOT and FIXED_SLOT[s] not in used:
            out[s] = SERIES[FIXED_SLOT[s]]
            used.add(FIXED_SLOT[s])
    free = [i for i in range(len(SERIES)) if i not in used]
    for s in services:
        if s not in out:
            out[s] = SERIES[free.pop(0)] if free else FAINT
    return out


def mix(c: str, a: float) -> str:
    """c blended over BG with opacity a, as an opaque colour (keeps SVG overlaps clean)."""
    c1 = [int(c[i:i + 2], 16) for i in (1, 3, 5)]
    c0 = [int(BG[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join("%02x" % round(x0 + (x1 - x0) * a) for x0, x1 in zip(c0, c1))


def figure(title, subtitle, services=(), col=None, nrows=1, ncols=1, size=(15, 6.0), legend_extra=(),
           sharex=False, sharey=False):
    fig, axes = plt.subplots(nrows, ncols, figsize=size, squeeze=False, sharex=sharex, sharey=sharey)
    head = 1.55 if (services or legend_extra) else 1.15
    fig.subplots_adjust(left=.08, right=.97, bottom=.75 / size[1], top=1 - (head + (.4 if nrows * ncols > 1 else .1)) / size[1],
                        hspace=.6, wspace=.25)
    fig.suptitle(title, x=.06 * 15 / size[0] * .9, y=1 - .2 / size[1], ha="left", va="top", fontsize=20,
                 fontfamily=["Sora", "DejaVu Sans"], fontweight="bold")
    fig.text(.06 * 15 / size[0] * .9, 1 - .68 / size[1], subtitle, ha="left", va="top", color=DIM, fontsize=9.5)
    handles = [plt.Line2D([0], [0], color=col[s], marker="o", lw=2, label=s) for s in services]
    handles += list(legend_extra)
    if handles:
        fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(.06 * 15 / size[0] * .9 - .006, 1 - .92 / size[1]),
                   frameon=False, ncol=len(handles), handlelength=1.6, columnspacing=1.8)
    for ax in axes.ravel():
        ax.grid(True, axis="both")
        ax.tick_params(length=0, pad=6)
    return fig, axes.ravel()


def plain(v, _pos=None):
    if v == 0:
        return "0"
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:g}M"
    if a >= 1e3:
        return f"{v / 1e3:g}k"
    return f"{v:g}"


def dur(v, _pos=None):
    """Seconds as an axis label."""
    if v <= 0:
        return "0"
    if v < 1:
        return f"{v * 1000:g} ms"
    if v < 120:
        return f"{v:g} s"
    return f"{v / 60:g} min"


def ms_fmt(v, _pos=None):
    if v <= 0:
        return "0"
    if v >= 1000:
        return f"{v / 1000:g} s"
    return f"{v:g}"


def log_axis(ax, axis="x", fmt=plain, span=None):
    """Log scale with plain labels; 1-2-5 ticks unless the range spans more than about two decades."""
    a = ax.xaxis if axis == "x" else ax.yaxis
    (ax.set_xscale if axis == "x" else ax.set_yscale)("log")
    subs = (1.0, 2.0, 5.0) if not span or span[1] / max(span[0], 1e-12) < 300 else (1.0,)
    a.set_major_locator(LogLocator(base=10, subs=subs))
    a.set_major_formatter(FuncFormatter(fmt))
    a.set_minor_locator(NullLocator())
    a.set_minor_formatter(NullFormatter())


def save(fig, out: pathlib.Path, name: str) -> str:
    p = out / "plots" / (name + ".svg")
    p.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(p, metadata={"Date": None})
    plt.close(fig)
    return "plots/%s.svg" % name


def write_csv(out: pathlib.Path, name: str, head: list[str], rows) -> str:
    p = out / "data" / (name + ".csv")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(head)
        w.writerows(rows)
    return "data/%s.csv" % name


def fmt_s(v) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "–"
    if v == 0:
        return "0"
    if v < 0.01:
        return f"{v * 1000:.1f} ms"
    if v < 1:
        return f"{v * 1000:.0f} ms"
    if v < 10:
        return f"{v:.2f} s"
    if v < 120:
        return f"{v:.1f} s"
    return f"{v / 60:.1f} min"


# --------------------------------------------------------------------------
# Figures. Each takes the prepared model of site.py (see site.Model).
# --------------------------------------------------------------------------

def tasks_figure(m, out) -> tuple[str, str] | None:
    """Every repetition of every wallet task: one row per task, one dot per repetition."""
    rows = [t for t in m.tasks if t["unit"] == "s" and any(t["reps"].get(s) for s in m.services)]
    if not rows:
        return None
    svcs, col = m.services, m.col
    n = len(rows)
    size = (15, 1.9 + .62 * n)
    fig, (ax,) = figure("Wallet tasks", "Time a wallet waits, one dot per repetition · large dot: median · bar: 95% CI "
                        "of the median · log scale · lower is better", svcs, col, size=size)
    fig.subplots_adjust(left=.25, right=.97)
    gap = .8 / max(1, len(svcs))
    lo, hi = math.inf, 0
    csvrows = []
    for i, t in enumerate(rows):
        y0 = n - 1 - i
        if i % 2 == 0:
            ax.axhspan(y0 - .5, y0 + .5, color=PANEL, zorder=0, lw=0)
        for j, s in enumerate(svcs):
            v = [x for x in t["reps"].get(s, []) if x is not None and x > 0]
            if not v:
                continue
            y = y0 + (len(svcs) - 1) / 2 * gap - j * gap
            jit = np.linspace(-gap * .18, gap * .18, len(v)) if len(v) > 1 else [0]
            ax.scatter(v, y + np.asarray(jit), s=12, color=mix(col[s], .55), lw=0, zorder=2)
            st = t["stats"].get(s) or {}
            med, ci = st.get("p50"), st.get("median_ci95")
            if ci:
                ax.plot(ci, [y, y], color=col[s], lw=2, solid_capstyle="round", zorder=3)
            if med:
                ax.scatter([med], [y], s=64, color=col[s], edgecolor=BG, linewidth=1.6, zorder=4)
                nf = (t.get("failed") or {}).get(s)
                ax.annotate(fmt_s(med) + (f"  ⚠ {nf}/{len(v)} incomplete" if nf else ""),
                            (max(v + ([ci[1]] if ci else [])), y), xytext=(8, 0),
                            textcoords="offset points", va="center", fontsize=8, color=DIM)
            lo, hi = min(lo, min(v)), max(hi, max(v))
            csvrows += [[t["id"], t["step"], t["metric"], s, k, x] for k, x in enumerate(t["reps"].get(s, []))]
    log_axis(ax, "x", dur, (lo, hi))
    anyfail = any(n for t in rows for n in (t.get("failed") or {}).values())
    ax.set_xlim(lo / 1.6, hi * (6 if anyfail else 2.6))
    ax.set_ylim(-.5, n - .5)
    ax.set_yticks(range(n))
    ax.set_yticklabels([t["label"] for t in reversed(rows)], color=INK, fontsize=9.5)
    ax.grid(False, axis="y")
    ax.spines["left"].set_visible(False)
    ax.set_xlabel("Seconds · log")
    data = write_csv(out, "tasks", ["task", "step", "metric", "service", "rep", "value"], csvrows)
    return save(fig, out, "tasks"), data


def phases_figure(m, out) -> tuple[str, str] | None:
    """Where the time of a sync goes: before the first block request, the block download, after it."""
    steps = [t for t in m.tasks if t.get("phases")]
    if not steps:
        return None
    svcs, col = m.services, m.col
    ncols = 3
    nrows = math.ceil(len(steps) / ncols)
    shade = {"prelude_s": 1.0, "blocks_phase_s": .55, "tail_s": .28}
    names = {"prelude_s": "before the first block request", "blocks_phase_s": "block download",
             "tail_s": "after the last block"}
    legend = [Patch(facecolor=mix(INK, a * .85), edgecolor="none", label=names[k]) for k, a in shade.items()]
    fig, axes = figure("Where the time goes", "Median of each phase over the repetitions · the shade of a bar is the "
                       "phase, its colour the service", (), col, nrows, ncols, size=(15, 1.6 + 2.15 * nrows),
                       legend_extra=legend)
    fig.subplots_adjust(left=.09, wspace=.32)
    csvrows = []
    for ax, t in zip(axes, steps):
        ax.set_title(t["label"], color=INK, fontsize=10)
        total = max((sum(v or 0 for v in ph.values()) for ph in t["phases"].values()), default=0)
        for j, s in enumerate(svcs):
            ph = t["phases"].get(s)
            if not ph:
                continue
            y = len(svcs) - 1 - j
            x = 0
            for k in ("prelude_s", "blocks_phase_s", "tail_s"):
                w = ph.get(k) or 0
                ax.barh(y, w, left=x, height=.62, color=mix(col[s], shade[k]), edgecolor=BG, linewidth=1.5)
                x += w
                csvrows.append([t["step"], s, k, w])
            ax.annotate(fmt_s(x), (x, y), xytext=(6, 0), textcoords="offset points", va="center", fontsize=8,
                        color=DIM)
            pre = ph.get("prelude_s") or 0
            if x and pre / max(total, x) > .22:
                ax.annotate(fmt_s(pre), (pre / 2, y), ha="center", va="center", fontsize=7.5, color=BG)
        ax.set_xlim(0, total * 1.28 if total else 1)
        ax.set_yticks(range(len(svcs)))
        ax.set_yticklabels(list(reversed(svcs)), fontsize=8.5, color=INK)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=4, steps=[1, 2, 2.5, 5, 10]))
        ax.xaxis.set_major_formatter(FuncFormatter(dur))
        ax.grid(False, axis="y")
        ax.spines["left"].set_visible(False)
    for ax in axes[len(steps):]:
        ax.set_visible(False)
    data = write_csv(out, "phases", ["step", "service", "phase", "median_s"], csvrows)
    return save(fig, out, "phases"), data


def download_figure(m, out) -> tuple[str, str] | None:
    """Blocks received against time since the repetition started, one line per repetition."""
    steps = [t for t in m.tasks if t.get("phases")]
    if not steps:
        return None
    ids = {t["step"] for t in steps}
    calls = [c for c in m.calls if c.get("step") in ids]
    gbr = [c for c in calls if c["method"] == "GetBlockRange"]
    msgs = rawio.read_messages(m.run_dir, {c["call_id"] for c in gbr})
    start = {}
    for c in calls:
        k = (c["step"], c["service"], c["rep"])
        start[k] = min(start.get(k, math.inf), c["started_s"])
    curves = defaultdict(list)
    for c in gbr:
        k = (c["step"], c["service"], c["rep"])
        curves[k] += [c["started_s"] - start[k] + t / 1000.0 for t, _b in msgs.get(c["call_id"], [])]
    svcs, col = m.services, m.col
    ncols = 3
    nrows = math.ceil(len(steps) / ncols)
    fig, axes = figure("Block download", "Blocks received since the repetition started, one line per repetition · "
                       "a late start is time spent before the first block · a flat stretch is a stall",
                       svcs, col, nrows, ncols, size=(15, 1.7 + 3.0 * nrows))
    fig.subplots_adjust(left=.07, wspace=.22)
    csvrows = []
    for ax, t in zip(axes, steps):
        ax.set_title(t["label"], color=INK, fontsize=10)
        xmax = 0
        for s in svcs:
            for (st, sv, rep), ts in sorted(curves.items(), key=lambda kv: str(kv[0])):
                if st != t["step"] or sv != s or not ts:
                    continue
                ts = np.sort(np.asarray(ts))
                ys = np.arange(1, ts.size + 1)
                if ts.size > 400:  # thin the line, keep the shape
                    idx = np.unique(np.linspace(0, ts.size - 1, 400).astype(int))
                    ts, ys = ts[idx], ys[idx]
                ax.plot(np.concatenate([[0, ts[0]], ts]), np.concatenate([[0, 0], ys]), color=col[s], lw=1.5,
                        alpha=.75, solid_joinstyle="round")
                xmax = max(xmax, ts[-1])
                csvrows += [[t["step"], s, rep, round(float(a), 4), int(b)] for a, b in zip(ts[::10], ys[::10])]
        ax.set_xlim(0, xmax * 1.03 if xmax else 1)
        ax.set_ylim(0, None)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=5, steps=[1, 2, 2.5, 5, 10]))
        ax.xaxis.set_major_formatter(FuncFormatter(dur))
        ax.yaxis.set_major_formatter(FuncFormatter(plain))
    for i, ax in enumerate(axes):
        if i % ncols == 0 and ax.get_visible():
            ax.set_ylabel("Blocks received")
    for ax in axes[len(steps):]:
        ax.set_visible(False)
    data = write_csv(out, "download", ["step", "service", "rep", "seconds", "blocks"], csvrows)
    return save(fig, out, "download"), data


def latency_figure(m, out) -> tuple[str, str] | None:
    """Latency distribution per RPC, all steps pooled, warm calls only."""
    panels = m.latency_panels
    if not panels:
        return None
    svcs, col = m.services, m.col
    ncols = 4
    nrows = math.ceil(len(panels) / ncols)
    rtt = m.rtt_ms
    sub = ("Share of calls answered within x ms · warm calls of every step pooled · log scale · "
           "further left is faster")
    if rtt:
        sub += f" · dotted line: network round trip ({rtt:.1f} ms)"
    fig, axes = figure("RPC latency", sub, svcs, col, nrows, ncols, size=(15, 1.7 + 2.9 * nrows))
    fig.subplots_adjust(left=.06, wspace=.18)
    csvrows = []
    for ax, (title, key) in zip(axes, panels):
        ax.set_title(title, color=INK, fontsize=10)
        lo, hi = math.inf, 0
        for s in svcs:
            v = np.sort(np.asarray(m.latency.get((key, s), []), dtype=float))
            if v.size == 0:
                continue
            y = np.arange(1, v.size + 1) / v.size
            if v.size > 600:
                idx = np.unique(np.linspace(0, v.size - 1, 600).astype(int))
                v, y = v[idx], y[idx]
            ax.step(np.concatenate([[v[0]], v]), np.concatenate([[0], y]), where="post", color=col[s], lw=2)
            lo, hi = min(lo, v[0]), max(hi, v[-1])
            full = np.asarray(m.latency[(key, s)], dtype=float)
            csvrows.append([key, s, full.size] + [round(float(np.percentile(full, q)), 3) for q in (5, 25, 50, 75, 95, 99)])
        if rtt:
            ax.axvline(rtt, color=DIM, ls=":", lw=1)
            lo = min(lo, rtt)
        log_axis(ax, "x", ms_fmt, (lo, hi) if math.isfinite(lo) else None)
        if math.isfinite(lo):
            ax.set_xlim(lo / 1.4, hi * 1.4)
        ax.set_ylim(0, 1.03)
        ax.yaxis.set_major_locator(FixedLocator([0, .5, .9]))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: "p%d" % round(v * 100) if v > 0 else "0"))
        ax.set_xlabel("ms · log")
    for ax in axes[len(panels):]:
        ax.set_visible(False)
    data = write_csv(out, "latency", ["rpc", "service", "n", "p5_ms", "p25_ms", "p50_ms", "p75_ms", "p95_ms", "p99_ms"],
                     csvrows)
    return save(fig, out, "latency"), data


def load_figure(m, out) -> tuple[str, str] | None:
    """Scale: wallets that start the same catch-up at the same moment, on their own connections."""
    sc = m.scale
    if not sc:
        return None
    svcs, col = m.services, m.col
    panels = [("aggregate_mbytes_per_s", "All wallets: MB/s ↑", "MB/s"),
              ("wallet_mbytes_per_s", "Per wallet: MB/s ↑", "MB/s"),
              ("first_block_ms", "First block, p50 ↓", "Milliseconds")]
    has_err = any(e.get("errors") for per in sc.values() for e in per.values())
    if has_err:
        panels.append(("error_rate", "Error rate ↓", "Share of calls"))
    fig, axes = figure("Wallets at once", "Each wallet catches up %s on its own connection; all start together · "
                       "line: median of the repetitions · band: its 95%% CI" % m.scale_behind
                       + ("" if has_err else " · no call failed at any level"),
                       svcs, col, 1, len(panels), size=(15, 5.4))
    fig.subplots_adjust(left=.06, wspace=.25)
    levels = sorted({int(k) for per in sc.values() for k in per})
    csvrows = []
    for ax, (key, title, ylabel) in zip(axes, panels):
        ax.set_title(title, color=INK, fontsize=10)
        for s in svcs:
            per = sc.get(s) or {}
            xs, ys, lo, hi = [], [], [], []
            for lv in levels:
                e = per.get(str(lv))
                if not e:
                    continue
                if key == "error_rate":
                    y, ci = e["errors"] / max(1, e["calls"]), None
                else:
                    d = e.get(key) or {}
                    y, ci = d.get("p50"), d.get("median_ci95")
                if y is None:
                    continue
                xs.append(lv)
                ys.append(y)
                lo.append(ci[0] if ci else y)
                hi.append(ci[1] if ci else y)
                csvrows.append([key, s, lv, y, lo[-1], hi[-1]])
            if xs:
                ax.fill_between(xs, lo, hi, color=col[s], alpha=.14, lw=0)
                ax.plot(xs, ys, "o-", color=col[s], lw=2, ms=6, mec=BG, mew=1.2)
        ax.set_xscale("log", base=2)
        ax.xaxis.set_major_locator(FixedLocator(levels))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: "%d" % v))
        ax.xaxis.set_minor_locator(NullLocator())
        ax.set_xlabel("Wallets at once")
        ax.set_ylabel(ylabel)
        ax.set_ylim(0, None)
        ax.yaxis.set_major_formatter(FuncFormatter(plain))
    data = write_csv(out, "load", ["metric", "service", "wallets", "median", "ci_low", "ci_high"], csvrows)
    return save(fig, out, "load"), data


def tips_figure(m, out) -> tuple[str, str] | None:
    """How often each service was behind the highest answer, and how late new blocks reached it."""
    lag, arr = m.tip_lag, m.tip_arrival
    if not lag:
        return None
    svcs = m.services
    panels = [("Blocks behind the highest service", lag, ["at the tip", "1 behind", "2 behind", "3 or more"])]
    if arr and any(arr.values()):
        panels.append(("New block seen, snapshots after the first service", arr,
                       ["same snapshot", "1 later", "2 later", "3 or more later"]))
    legend = [Patch(facecolor=c, edgecolor="none", label=l) for c, l in zip(RAMP, panels[0][2])]
    sub = "Share of snapshots: every service asked GetLatestBlock at the same moment"
    if len(panels) > 1:
        sub += " · right: per new block, snapshots %s apart" % m.observe_label
    fig, axes = figure("Tip freshness", sub, (), m.col, 1, len(panels), size=(15, 1.6 + .55 * len(svcs) + 1.6),
                       legend_extra=legend)
    fig.subplots_adjust(left=.09, wspace=.28)
    csvrows = []
    for ax, (title, src, _labels) in zip(axes, panels):
        ax.set_title(title, color=INK, fontsize=10)
        for j, s in enumerate(svcs):
            v = src.get(s) or []
            if not v:
                continue
            y = len(svcs) - 1 - j
            counts = [sum(1 for x in v if x == k) for k in (0, 1, 2)] + [sum(1 for x in v if x >= 3)]
            x = 0
            for k, c in enumerate(counts):
                w = c / len(v)
                ax.barh(y, w, left=x, height=.6, color=RAMP[k], edgecolor=BG, linewidth=1.5)
                if w >= .07:
                    ax.annotate("%d%%" % round(w * 100), (x + w / 2, y), ha="center", va="center", fontsize=8,
                                color=INK if k < 2 else BG)
                x += w
                csvrows.append([title, s, k, c, len(v)])
            ax.annotate("n=%d" % len(v), (1, y), xytext=(6, 0), textcoords="offset points", va="center",
                        fontsize=8, color=DIM)
        ax.set_xlim(0, 1.09)
        ax.set_yticks(range(len(svcs)))
        ax.set_yticklabels(list(reversed(svcs)), color=INK, fontsize=9)
        ax.xaxis.set_major_locator(FixedLocator([0, .25, .5, .75, 1]))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: "%d%%" % round(v * 100)))
        ax.grid(False, axis="y")
        ax.spines["left"].set_visible(False)
    data = write_csv(out, "tips", ["panel", "service", "bucket", "count", "total"], csvrows)
    return save(fig, out, "tips"), data
