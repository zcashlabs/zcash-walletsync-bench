"""Compare two geo sweeps run the same way at different times (before and after a server change).

    zcash-walletsync-bench geo-compare results/<old sweep> results/<new sweep>

Writes <new>/compare.json, <new>/compare.txt and <new>/site/compare.html (with plots/ and data/). It
reads both sweeps' cities.jsonl, syncs.jsonl and phase-2 run directories; it sends nothing.

Phase 1 numbers are GetLatestBlock on a warm connection minus the tunnel baseline of that sweep, so a
change of Proton server or tunnel between the sweeps cancels out as far as it can. Phase-2 call times
are also shown minus the round trip of that run (median GetLatestBlock of the catch-up step, same
service), which approximates the time the server spent.
"""

from __future__ import annotations

import json
import pathlib
import statistics

from . import site_plots as sp
from .geosite import COMP, FASTZ_COL, Geo, E, _table, dot, ms
from .rawio import read_jsonl as raw_jsonl
from .stats import bootstrap_ci, ratio_ci
from .brand import copy_brand

REGIONS = ("eu", "ap", "usw", "use", "sa", "jp", "lon")
REGION_NAME = {"eu": "eu (Frankfurt)", "ap": "ap (Singapore)", "usw": "usw (Hillsboro)", "use": "use (Virginia)",
               "sa": "sa (São Paulo)", "jp": "jp (Tokyo)", "lon": "lon (London)"}
ZR_COL = COMP["zecrocks"][1]

# Phase-2 metrics: key -> (label, unit, how)
METRICS = [
    ("first_wall", "First sync, 10,000 blocks", "s"),
    ("catch_wall", "Catch-up, 1 day behind", "s"),
    ("roots_sapling", "GetSubtreeRoots Sapling, full list (first sync)", "ms"),
    ("roots_orchard", "GetSubtreeRoots Orchard, full list (first sync)", "ms"),
    ("roots_tail", "GetSubtreeRoots from the wallet's index (catch-up)", "ms"),
    ("tree_first", "GetTreeState (first sync)", "ms"),
    ("tree_catch", "GetTreeState (catch-up)", "ms"),
    ("rtt", "Round trip: GetLatestBlock (catch-up)", "ms"),
]


SHORT = {"first_wall": "First sync (10k blocks)", "catch_wall": "Catch-up (1 day)",
         "roots_sapling": "GetSubtreeRoots, Sapling", "tree_first": "GetTreeState (first sync)"}


SRV_KEYS = ("roots_tail", "roots_sapling", "roots_orchard", "tree_catch", "tree_first")
SRV_LABEL = {"roots_tail": "GetSubtreeRoots, catch-up (from the wallet's index)",
             "roots_sapling": "GetSubtreeRoots Sapling, full list", "roots_orchard": "GetSubtreeRoots Orchard, full list",
             "tree_catch": "GetTreeState, catch-up", "tree_first": "GetTreeState, first sync (beside block streams)"}


def _q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _step(steps, prefix):
    return next((k for k in sorted(steps) if k.startswith(prefix + "/")), None)


# ------------------------------------------------------------------ phase 2 per run

def run_metrics(run: pathlib.Path, summary: dict) -> dict:
    """{service: {metric: {"p50": v, "values": [...]}}} for one phase-2 run."""
    steps = summary.get("steps") or {}
    out: dict[str, dict] = {}
    for prefix, key in (("first-sync", "first_wall"), ("catch-up", "catch_wall")):
        sid = _step(steps, prefix)
        for svc, ps in ((steps.get(sid) or {}).get("per_service") or {}).items():
            vals = [v for v, ok in zip((ps.get("per_rep") or {}).get("wall_s") or [],
                                       (ps.get("per_rep") or {}).get("ok") or [True] * 99) if v is not None and ok]
            out.setdefault(svc, {})[key] = {"p50": med(vals), "values": vals,
                                            "stalls": ((ps.get("block_streams") or {}).get("stalls") or 0),
                                            "failed": ps.get("reps_failed", 0)}
    calls = raw_jsonl(run, "calls", stress=False) if run and run.exists() else []
    floor: dict[str, float] = {}
    for c in calls:
        if c.get("ok") and c.get("method") == "GetLatestBlock" and c.get("latency_ms") is not None:
            floor[c["service"]] = min(floor.get(c["service"], 1e9), c["latency_ms"])
    buckets: dict[tuple, list] = {}
    server: dict[tuple, list] = {}
    for c in calls:
        if not c.get("ok") or not c.get("step"):
            continue
        scen, m, pur = c.get("scenario"), c.get("method"), c.get("purpose") or ""
        key = None
        if m == "GetSubtreeRoots" and scen == "first-sync" and pur.endswith("sapling"):
            key = "roots_sapling"
        elif m == "GetSubtreeRoots" and scen == "first-sync" and pur.endswith("orchard"):
            key = "roots_orchard"
        elif m == "GetSubtreeRoots" and scen == "catch-up":
            key = "roots_tail"
        elif m == "GetTreeState":
            key = "tree_first" if scen == "first-sync" else "tree_catch" if scen == "catch-up" else None
        elif m == "GetLatestBlock" and scen == "catch-up" and not c.get("cold"):
            key = "rtt"
        if key:
            buckets.setdefault((c["service"], key), []).append(c["latency_ms"])
            if key != "rtt" and c["service"] in floor and c.get("ttfb_ms") is not None:
                server.setdefault((c["service"], key), []).append(c["ttfb_ms"] - floor[c["service"]])
    for (svc, key), vals in buckets.items():
        out.setdefault(svc, {})[key] = {"p50": med(vals), "values": vals,
                                        "server": server.get((svc, key), [])}
    for svc, m in out.items():
        rtt = (m.get("rtt") or {}).get("p50")
        for key in ("roots_sapling", "roots_orchard", "roots_tail", "tree_first", "tree_catch"):
            if key in m and rtt is not None:
                m[key]["excess"] = m[key]["p50"] - rtt
    return out


def sync_table(g: Geo) -> dict:
    """{city: {"why", "name", "region": {service: fastz region or None}, "m": run_metrics}}."""
    out = {}
    for sync in g.syncs:
        if not sync["summary"]:
            continue
        rec = g.recs.get(sync["key"]) or {}
        regions = {}
        for svc in sync["sy"].get("services", []):
            regions[svc] = rec.get("fastz_dns_region") if svc == "fastz" else \
                svc[len("fastz-"):] if svc.startswith("fastz-") else None
        out[sync["key"]] = {"why": sync["sy"].get("why"), "name": "%s, %s" % (rec.get("name"), rec.get("country")),
                            "region": regions, "server": sync["sy"].get("server"),
                            "m": run_metrics(sync["run"], sync["summary"])}
    return out


# ------------------------------------------------------------------ phase 1

def city_pairs(old: Geo, new: Geo) -> list[dict]:
    o = {r["key"]: r for r in old.rows}
    out = []
    for r in new.rows:
        a = o.get(r["key"])
        if not a:
            continue
        row = {"key": r["key"], "name": r["name"],
               "server": (a["rec"].get("server"), r["rec"].get("server")),
               "tunnel": (a["s"]["tunnel_p50_ms"], r["s"]["tunnel_p50_ms"]),
               "dns": (a["rec"].get("fastz_dns_region"), r["rec"].get("fastz_dns_region")),
               "fastz": tuple(x["s"]["targets"].get("fastz", {}).get("p50_minus_tunnel") for x in (a, r)),
               "regions": {g: tuple(x["s"]["targets"].get("fastz-" + g, {}).get("p50_minus_tunnel") for x in (a, r))
                           for g in REGIONS},
               "zr": (old.delta(a, "zecrocks"), new.delta(r, "zecrocks")),
               "zr_target": tuple((x["s"]["competitors"].get("zecrocks") or {}).get("target") for x in (a, r)),
               "zl": (old.delta(a, "zcashlabs"), new.delta(r, "zcashlabs")),
               "qualifies": (a["s"]["qualifies"], r["s"]["qualifies"]),
               "misteered": tuple(bool((x["s"].get("steering") or {}).get("misteered")) for x in (a, r))}
        out.append(row)
    return out


def region_changes(pairs: list[dict]) -> dict:
    """Per fastz region: median change (new - old) of its latency minus tunnel, over all cities and over
    the cities DNS sends to it, with a bootstrap CI."""
    out = {}
    for g in REGIONS:
        res = {}
        for scope, rows in (("all", pairs), ("steered", [p for p in pairs if p["dns"][1] == g])):
            d = [p["regions"][g][1] - p["regions"][g][0] for p in rows
                 if None not in p["regions"][g]]
            if not d:
                res[scope] = {"n": 0}
                continue
            res[scope] = {"n": len(d), "median_ms": round(statistics.median(d), 2),
                          "ci95_ms": [round(x, 2) for x in bootstrap_ci(d)] if len(d) >= 5 else None,
                          "old_p50": med(p["regions"][g][0] for p in rows),
                          "new_p50": med(p["regions"][g][1] for p in rows)}
        out[g] = res
    return out


# ------------------------------------------------------------------ figures

def delta_change_figure(pairs, out):
    rows = [p for p in pairs if p["zr"][0].get("n") and p["zr"][1].get("n")]
    if not rows:
        return None
    rows.sort(key=lambda p: p["zr"][1]["median_ms"])
    n = len(rows)
    import matplotlib.ticker as mt
    old_col = sp.mix(ZR_COL, .45)
    fig, (ax,) = sp.figure("zec.rocks minus fastz, before and after",
                           "Median of (zec.rocks - fastz) per round, GetLatestBlock. Left of 0: zec.rocks faster.",
                           size=(13, 2.2 + 0.22 * n),
                           legend_extra=[sp.plt.Line2D([0], [0], color=old_col, marker="o", mfc="none", lw=0,
                                                       label="previous sweep"),
                                         sp.plt.Line2D([0], [0], color=ZR_COL, marker="o", lw=0, label="this sweep")])
    csv = []
    for i, p in enumerate(rows):
        y = n - 1 - i
        a, b = p["zr"][0]["median_ms"], p["zr"][1]["median_ms"]
        ax.plot([a, b], [y, y], color=sp.mix(ZR_COL, .3), lw=2, zorder=2, solid_capstyle="round")
        ax.plot([a], [y], "o", ms=6, mfc=sp.BG, mec=old_col, mew=1.6, zorder=3)
        ax.plot([b], [y], "o", ms=6, color=ZR_COL, mec=sp.BG, mew=1.5, zorder=4)
        csv.append([p["key"], p["name"], a, b, p["zr"][0].get("verdict"), p["zr"][1].get("verdict")])
    ax.axvline(0, color=FASTZ_COL, lw=1.5, zorder=1)
    ax.set_xscale("symlog", linthresh=10)
    ax.xaxis.set_major_locator(mt.FixedLocator([-300, -100, -30, -10, -5, 0, 5, 10, 30, 100, 300]))
    ax.xaxis.set_major_formatter(mt.FuncFormatter(lambda v, _p: "0" if v == 0 else "%+g" % v))
    ax.xaxis.set_minor_locator(mt.NullLocator())
    xs = [x for c in csv for x in c[2:4]]
    ax.set_xlim(min(min(xs) * 1.5, -15), max(max(xs) * 1.5, 15))
    ax.set_yticks(range(n))
    ax.set_yticklabels([p["name"] for p in reversed(rows)], fontsize=8.5, color=sp.INK)
    ax.set_ylim(-0.7, n - 0.3)
    ax.grid(False, axis="y")
    ax.set_xlabel("ms (linear within ±10, logarithmic beyond)")
    fig.subplots_adjust(left=.2, bottom=0.75 / (2.2 + 0.22 * n))
    return sp.save(fig, out, "zecrocks-delta-change"), sp.write_csv(
        out, "zecrocks-delta-change", ["city", "name", "old_median_ms", "new_median_ms", "old_verdict", "new_verdict"],
        csv)


def sync_change_figure(old_t, new_t, out):
    keys = [k for k in new_t if k in old_t]
    panels = [m for m in METRICS if m[0] in ("first_wall", "catch_wall", "roots_sapling", "tree_first")]
    lines = []
    for k in sorted(keys, key=lambda k: new_t[k]["name"]):
        for svc in ("fastz", "zecrocks"):
            if svc in new_t[k]["m"] and svc in old_t[k]["m"]:
                lines.append((k, svc))
    if not lines:
        return None
    import matplotlib.ticker as mt
    n = len(lines)
    cols = {"fastz": FASTZ_COL, "zecrocks": ZR_COL}
    fig, axes = sp.figure("Phase-2 syncs, before and after",
                          "Same cities, same services. Medians of 3 repetitions (sync time) or of every call "
                          "(GetSubtreeRoots, GetTreeState; including the round trip). Hollow: previous sweep; "
                          "filled: this sweep.", ncols=len(panels), size=(16, 2.6 + 0.26 * n), sharey=True,
                          legend_extra=[sp.plt.Line2D([0], [0], color=cols[s], marker="o", lw=0, label=lbl)
                                        for s, lbl in (("fastz", "fastz"), ("zecrocks", "zec.rocks"))])
    csv = []
    for ax, (key, label, unit) in zip(axes, panels):
        for i, (k, svc) in enumerate(lines):
            y = n - 1 - i
            a = (old_t[k]["m"][svc].get(key) or {}).get("p50")
            b = (new_t[k]["m"][svc].get(key) or {}).get("p50")
            c = cols[svc]
            if a is not None and b is not None:
                ax.plot([a, b], [y, y], color=sp.mix(c, .3), lw=2, zorder=2)
            if a is not None:
                ax.plot([a], [y], "o", ms=6, mfc=sp.BG, mec=sp.mix(c, .55), mew=1.6, zorder=3)
            if b is not None:
                ax.plot([b], [y], "o", ms=6, color=c, mec=sp.BG, mew=1.5, zorder=4)
            csv.append([k, svc, key, a, b])
        ax.set_xscale("log")
        ax.xaxis.set_major_formatter(mt.FuncFormatter(sp.dur if unit == "s" else
                                                      (lambda v, _p: sp.dur(v / 1000))))
        ax.xaxis.set_minor_formatter(mt.NullFormatter())
        ax.set_title(SHORT[key], fontsize=10)
        ax.grid(False, axis="y")
    axes[0].set_yticks(range(n))
    axes[0].set_yticklabels(["%s · %s" % (new_t[k]["name"].split(",")[0], "zec.rocks" if s == "zecrocks" else
                                          s + ("" if s != "fastz" else " " + (new_t[k]["region"].get("fastz") or "")))
                             for k, s in reversed(lines)], fontsize=8.5, color=sp.INK)
    axes[0].set_ylim(-0.7, n - 0.3)
    fig.subplots_adjust(left=.14, wspace=.12, bottom=0.8 / (2.6 + 0.26 * n))
    return sp.save(fig, out, "sync-change"), sp.write_csv(out, "sync-change",
                                                           ["city", "service", "metric", "old", "new"], csv)


# ------------------------------------------------------------------ build

def _fmt(v, unit):
    if v is None:
        return "–"
    return sp.fmt_s(v) if unit == "s" else "%.0f ms" % v if v >= 100 else "%.1f ms" % v


def _chg(a, b, unit):
    if a is None or b is None or not a:
        return "–"
    r = b / a
    cls = "good" if r < 0.9 else "err" if r > 1.1 else ""
    return f'<span class="{cls}">{r:.2f}×</span>'


def zr_ratio(t, key):
    """zec.rocks / fastz sync time for one city, with CI."""
    m = t["m"]
    a = ((m.get("zecrocks") or {}).get(key) or {}).get("values")
    b = ((m.get("fastz") or {}).get(key) or {}).get("values")
    if not a or not b:
        return None
    return ratio_ci(a, b)


def build(old_dir, new_dir) -> str:
    sp.setup()
    old, new = Geo(old_dir), Geo(new_dir)
    pairs = city_pairs(old, new)
    regions = region_changes(pairs)
    old_t, new_t = sync_table(old), sync_table(new)
    both = [k for k in new_t if k in old_t]

    # Per-region phase-2 aggregate: fastz services grouped by the region that served them.
    agg = {}
    for label, t in (("old", old_t), ("new", new_t)):
        for k, row in t.items():
            if k not in both:
                continue
            for svc, rg in row["region"].items():
                if rg is None or svc not in row["m"]:
                    continue
                for key, _l, _u in METRICS:
                    v = row["m"][svc].get(key) or {}
                    if v.get("p50") is not None:
                        agg.setdefault(rg, {}).setdefault(key, {}).setdefault(label, []).append(
                            v.get("excess", v["p50"]) if key.startswith(("roots", "tree")) else v["p50"])
    agg_rows = {rg: {key: {lab: med(vs) for lab, vs in d.items()} for key, d in m.items()} for rg, m in agg.items()}

    srv = {}
    for label, t in (("old", old_t), ("new", new_t)):
        for k, row in t.items():
            for svc, rg in row["region"].items():
                grp = rg if rg else svc if svc in COMP else None
                if grp is None or svc not in row["m"]:
                    continue
                for key in SRV_KEYS:
                    v = (row["m"][svc].get(key) or {}).get("server") or []
                    srv.setdefault(grp, {}).setdefault(key, {}).setdefault(label, []).extend(v)
    srv_rows = {g: {key: {lab: {"n": len(v), "median": med(v), "p75": _q(v, .75), "slow_share": (
        sum(x > 50 for x in v) / len(v)) if v else None} for lab, v in d.items()} for key, d in m.items()}
        for g, m in srv.items()}

    zr_old = sum(1 for r in old.rows if r["s"]["competitors"].get("zecrocks", {}).get("vs_fastz", {}).get(
        "verdict") == "faster")
    zr_new = sum(1 for r in new.rows if r["s"]["competitors"].get("zecrocks", {}).get("vs_fastz", {}).get(
        "verdict") == "faster")
    flips_to = [p for p in pairs if p["zr"][0].get("verdict") != "faster" and p["zr"][1].get("verdict") == "faster"]
    flips_away = [p for p in pairs if p["zr"][0].get("verdict") == "faster" and p["zr"][1].get("verdict") != "faster"]
    dz = [p["zr"][1]["median_ms"] - p["zr"][0]["median_ms"] for p in pairs
          if p["zr"][0].get("n") and p["zr"][1].get("n")]
    dz_ci = bootstrap_ci(dz) if len(dz) >= 5 else None
    dfz = [p["fastz"][1] - p["fastz"][0] for p in pairs if None not in p["fastz"]]

    # ---------------- text
    L = ["Geo sweep comparison: %s (previous) -> %s (this)" % (old.dir.name, new.dir.name),
         "Phase 1: %d cities in both (previous %d, this %d)" % (len(pairs), len(old.rows), len(new.rows)),
         "zec.rocks faster than fastz (DNS-steered, beyond the CI): %d -> %d cities" % (zr_old, zr_new),
         "  now faster there, before not: %s" % (", ".join(p["name"] for p in flips_to) or "none"),
         "  no longer faster: %s" % (", ".join(p["name"] for p in flips_away) or "none"),
         "Median change of (zec.rocks - fastz) per city: %+.1f ms%s (positive: fastz gained)" % (
             med(dz) or 0, " [%+.1f, %+.1f]" % dz_ci if dz_ci else ""),
         "Median change of fastz latency minus tunnel: %+.1f ms over %d cities" % (med(dfz) or 0, len(dfz)), "",
         "Per fastz region, GetLatestBlock minus tunnel, change (new - old), ms:"]
    for g, r in regions.items():
        a, s = r["all"], r["steered"]
        L.append("  %-16s all cities: %s   cities DNS sends there: %s" % (
            REGION_NAME[g],
            "%+.1f (n=%d)" % (a["median_ms"], a["n"]) if a.get("n") else "-",
            "%+.1f (n=%d, %.1f -> %.1f)" % (s["median_ms"], s["n"], s["old_p50"], s["new_p50"]) if s.get("n") else "-"))
    L += ["", "Phase 2: %d cities in both sweeps" % len(both)]
    for k in sorted(both, key=lambda k: new_t[k]["name"]):
        L.append("  %s (%s)" % (new_t[k]["name"], new_t[k]["why"]))
        for svc in sorted(set(old_t[k]["m"]) | set(new_t[k]["m"])):
            cells = []
            for key, lab, unit in METRICS:
                a = ((old_t[k]["m"].get(svc) or {}).get(key) or {}).get("p50")
                b = ((new_t[k]["m"].get(svc) or {}).get(key) or {}).get("p50")
                if a is None and b is None:
                    continue
                f = (lambda v: "-" if v is None else "%.2fs" % v) if unit == "s" else \
                    (lambda v: "-" if v is None else "%.0f" % v)
                cells.append("%s %s->%s" % (key, f(a), f(b)))
            rg = new_t[k]["region"].get(svc)
            L.append("    %-10s %s" % (svc + (" " + rg if rg else ""), ", ".join(cells)))
        for key in ("first_wall", "catch_wall"):
            ro, rn = zr_ratio(old_t[k], key), zr_ratio(new_t[k], key)
            if ro or rn:
                L.append("    zec.rocks/fastz %-10s %s -> %s" % (key, "%.2f" % ro["ratio"] if ro else "-",
                                                                   "%.2f" % rn["ratio"] if rn else "-"))
    L += ["", "Server delay before the first message (first byte minus the fastest GetLatestBlock of that run), "
          "all phase-2 calls of each sweep, by fastz region / zec.rocks: median ms, p75 ms, share above 50 ms, "
          "previous -> this sweep"]
    for g in [*REGIONS, "zecrocks"]:
        if g not in srv_rows:
            continue
        L.append("  " + REGION_NAME.get(g, "zec.rocks"))
        for key in SRV_KEYS:
            d = srv_rows[g].get(key) or {}
            f = lambda x: "-" if not x or not x["n"] else "%.0f / %.0f / %.0f%% (n=%d)" % (  # noqa: E731
                x["median"], x["p75"], 100 * x["slow_share"], x["n"])
            L.append("    %-52s %s -> %s" % (SRV_LABEL[key], f(d.get("old")), f(d.get("new"))))
    L += ["", "Per region (phase-2 cities in both sweeps; call times minus the round trip):"]
    for rg in REGIONS:
        if rg not in agg_rows:
            continue
        cells = []
        for key, lab, unit in METRICS:
            v = agg_rows[rg].get(key) or {}
            if v:
                f = (lambda x: "-" if x is None else "%.2fs" % x) if unit == "s" else \
                    (lambda x: "-" if x is None else "%.0f" % x)
                cells.append("%s %s->%s" % (key, f(v.get("old")), f(v.get("new"))))
        L.append("  %-16s %s" % (REGION_NAME[rg], ", ".join(cells)))

    data = {"old": str(old.dir), "new": str(new.dir), "phase1": {
        "cities_in_both": len(pairs), "zecrocks_faster": [zr_old, zr_new],
        "now_faster": [p["key"] for p in flips_to], "no_longer_faster": [p["key"] for p in flips_away],
        "zecrocks_delta_change_median_ms": med(dz), "zecrocks_delta_change_ci95": dz_ci,
        "fastz_minus_tunnel_change_median_ms": med(dfz), "regions": regions, "cities": pairs},
        "phase2": {"cities_in_both": both,
                   "old": {k: {**v, "m": {s: {mk: {kk: vv for kk, vv in mv.items() if kk not in ("values", "server")}
                                              for mk, mv in mm.items()} for s, mm in v["m"].items()}}
                           for k, v in old_t.items()},
                   "new": {k: {**v, "m": {s: {mk: {kk: vv for kk, vv in mv.items() if kk not in ("values", "server")}
                                              for mk, mv in mm.items()} for s, mm in v["m"].items()}}
                           for k, v in new_t.items()},
                   "per_region": agg_rows, "server_delay": srv_rows}}
    (new.dir / "compare.json").write_text(json.dumps(data, indent=1, default=str))
    text = "\n".join(L)
    (new.dir / "compare.txt").write_text(text + "\n")

    # ---------------- page
    out = new.dir / "site"
    out.mkdir(parents=True, exist_ok=True)
    sections = []
    cards = [("zec.rocks closer than fastz", "%d → %d" % (zr_old, zr_new), "cities, previous → this sweep"),
             ("Δ zec.rocks − fastz, change", ms(med(dz)), "median per city; positive: fastz gained"),
             ("fastz latency change", ms(med(dfz)), "median per city, minus tunnel"),
             ("Phase-2 cities compared", "%d" % len(both), "same city, same services")]
    card_html = "".join(f'<div class="card"><span class="who">{E(a)}</span><b>{E(b)}</b><span>{E(c)}</span></div>'
                        for a, b, c in cards)

    # Syncs
    body = ["<p>The same Vizor catch-up (1 day behind) and first sync (10,000 blocks), 3 repetitions, in the cities "
            "of both sweeps' phase 2. GetSubtreeRoots and GetTreeState are per call, including the round trip; "
            "the per-region table subtracts it.</p>"]
    rows = []
    for g in [*REGIONS, "zecrocks"]:
        for key in SRV_KEYS:
            d = (srv_rows.get(g) or {}).get(key) or {}

            def c(x):
                if not x or not x["n"]:
                    return "–"
                return (f'{x["median"]:.0f} ms<em>p75 {x["p75"]:.0f} · {100 * x["slow_share"]:.0f}% over 50 ms · '
                        f'n={x["n"]}</em>')
            if d:
                col = ZR_COL if g == "zecrocks" else FASTZ_COL
                rows.append([f'{dot(col)}{E(REGION_NAME.get(g, "zec.rocks"))}', E(SRV_LABEL[key]),
                             c(d.get("old")), c(d.get("new"))])
    body.append("<h3>Server delay per call</h3><p>Time to the first message minus the fastest GetLatestBlock of the "
                "same run and service: what the server spent before answering (plus any queueing on the "
                "connection). All phase-2 calls of each sweep, grouped by the fastz region that served them.</p>")
    body.append(_table(["Server", "Call", "Previous sweep", "This sweep"], rows))
    made = sync_change_figure(old_t, new_t, out)
    if made:
        body.append(f'<figure><img loading="lazy" alt="Phase-2 metrics before and after" src="{made[0]}">'
                    f'<figcaption class="downloads"><a href="{made[1]}">Data (CSV)</a> · <a href="{made[0]}">SVG</a>'
                    '</figcaption></figure>')
    rows = []
    for k in sorted(both, key=lambda k: new_t[k]["name"]):
        for svc in sorted(set(old_t[k]["m"]) & set(new_t[k]["m"])):
            col = FASTZ_COL if svc.startswith("fastz") else COMP.get(svc, (svc, sp.DIM))[1]
            rg = new_t[k]["region"].get(svc)
            cells = [f'{E(new_t[k]["name"])}<em>{E(new_t[k]["why"] or "")}</em>',
                     f'{dot(col)}{E("zec.rocks" if svc == "zecrocks" else svc)}' + (f'<em>{E(rg)}</em>' if rg else "")]
            for key in ("first_wall", "catch_wall", "roots_sapling", "tree_first", "tree_catch"):
                unit = "s" if key.endswith("wall") else "ms"
                a = (old_t[k]["m"][svc].get(key) or {}).get("p50")
                b = (new_t[k]["m"][svc].get(key) or {}).get("p50")
                cells.append(f'{_fmt(a, unit)} → {_fmt(b, unit)}<em>{_chg(a, b, unit)}</em>')
            rows.append(cells)
    body.append('<details open><summary class="meta">Per city and service: previous → this sweep</summary>%s</details>'
                % _table(["City", "Service", "First sync", "Catch-up", "SubtreeRoots Sapling", "TreeState first sync",
                          "TreeState catch-up"], rows))
    rows = []
    for k in sorted(both, key=lambda k: new_t[k]["name"]):
        for key, lab in (("first_wall", "First sync"), ("catch_wall", "Catch-up")):
            ro, rn = zr_ratio(old_t[k], key), zr_ratio(new_t[k], key)
            if not (ro or rn):
                continue

            def cell(r):
                if not r or not r.get("ratio"):
                    return "–"
                v = "fastz" if r["significant"] and r["ratio"] > 1 else "zec.rocks" if r["significant"] else "tie"
                cls = {"fastz": "good", "zec.rocks": "err"}[v] if v != "tie" else ""
                ci = r.get("ci95")
                return f'<span class="{cls}">{r["ratio"]:.2f}×</span><em>{"[%.2f, %.2f] " % tuple(ci) if ci else ""}{v}</em>'
            rows.append([E(new_t[k]["name"]), lab, cell(ro), cell(rn)])
    body.append('<details open><summary class="meta">zec.rocks time / fastz time (above 1: fastz faster)</summary>%s'
                '</details>' % _table(["City", "Task", "Previous sweep", "This sweep"], rows))
    rows = []
    for rg in REGIONS:
        if rg not in agg_rows:
            continue
        cells = [E(REGION_NAME[rg])]
        for key in ("first_wall", "catch_wall", "roots_sapling", "roots_orchard", "tree_first", "tree_catch", "rtt"):
            unit = "s" if key.endswith("wall") else "ms"
            v = agg_rows[rg].get(key) or {}
            cells.append(f'{_fmt(v.get("old"), unit)} → {_fmt(v.get("new"), unit)}'
                         f'<em>{_chg(v.get("old"), v.get("new"), unit)}</em>')
        rows.append(cells)
    body.append('<details open><summary class="meta">Per fastz region (medians over the phase-2 cities it served; '
                'calls minus the round trip)</summary>%s</details>' % _table(
                    ["Region", "First sync", "Catch-up", "SubtreeRoots Sapling", "SubtreeRoots Orchard",
                     "TreeState first sync", "TreeState catch-up", "Round trip"], rows))
    sections.append(("syncs", "Wallet syncs", "".join(body)))

    # Latency
    body = ["<p>Phase 1, all cities in both sweeps: one warm GetLatestBlock per target per round, 24 rounds. "
            "The difference zec.rocks − fastz is paired within each round, so it does not depend on the tunnel.</p>"]
    made = delta_change_figure(pairs, out)
    if made:
        body.append(f'<figure><img loading="lazy" alt="zec.rocks minus fastz per city, before and after" '
                    f'src="{made[0]}"><figcaption class="downloads"><a href="{made[1]}">Data (CSV)</a> · '
                    f'<a href="{made[0]}">SVG</a></figcaption></figure>')
    rows = []
    for g in REGIONS:
        a, s = regions[g]["all"], regions[g]["steered"]

        def c(x):
            if not x.get("n"):
                return "–"
            ci = x.get("ci95_ms")
            return f'{ms(x["median_ms"])}<em>{"[%+.1f, %+.1f] · " % tuple(ci) if ci else ""}n={x["n"]}</em>'
        rows.append([E(REGION_NAME[g]), c(a), c(s),
                     f'{ms(s.get("old_p50"), False)} → {ms(s.get("new_p50"), False)}' if s.get("n") else "–"])
    body.append(_table(["fastz region", "Change, all cities<em>new − old, minus tunnel</em>",
                        "Change, cities DNS sends there", "Latency there<em>minus tunnel, median</em>"], rows))
    rows = []
    for p in sorted(pairs, key=lambda p: p["name"]):
        def d(x):
            if not x.get("n"):
                return "–"
            cls = {"faster": "err", "slower": "good"}.get(x.get("verdict"), "")
            return f'<span class="{cls}">{ms(x["median_ms"])}</span><em>{E(x.get("verdict", ""))}</em>'
        rows.append([f'{E(p["name"])}<em>{E(str(p["server"][0]))} → {E(str(p["server"][1]))}</em>',
                     "%.0f → %.0f ms" % tuple(x or 0 for x in p["tunnel"]),
                     "%s → %s" % tuple(E(x or "?") for x in p["dns"]),
                     f'{ms(p["fastz"][0], False)} → {ms(p["fastz"][1], False)}',
                     d(p["zr"][0]), d(p["zr"][1]), d(p["zl"][0]), d(p["zl"][1])])
    body.append('<details><summary class="meta">All %d cities</summary>%s</details>' % (len(pairs), _table(
        ["City<em>Proton server</em>", "Tunnel", "fastz DNS", "fastz<em>minus tunnel</em>", "Δ zec.rocks before",
         "Δ zec.rocks now", "Δ zcashlabs before", "Δ zcashlabs now"], rows)))
    sections.append(("latency", "Latency", "".join(body)))
    sections.append(("text", "Text summary", f"<pre>{E(text)}</pre>"))

    nav = "".join(f'<a href="#{i}">{E(t)}</a>' for i, t, _ in sections)
    h = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width, initial-scale=1">',
         "<title>Geo sweep comparison</title>", '<meta name="theme-color" content="#171415">',
         '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" '
         'href="https://fonts.gstatic.com" crossorigin>',
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Sora:wght@700;800&family=Manrope:'
         'wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">',
         '<link rel="icon" href="favicon.svg" type="image/svg+xml"><link rel="stylesheet" href="site.css"></head><body class="benchmarks-page">',
         f'<header><div class="wrap"><a class="brand" href="#top"><img src="mark.svg" alt="" width="40" height="40"><b>zcash-walletsync-bench</b><span>{E(new.dir.name)}</span>'
         '</a><nav><a href="index.html">This sweep</a><a href="#syncs">Syncs</a><a href="#latency">Latency</a></nav>'
         '</div></header><main class="wrap benchmark-report" id="top">',
         f'<div class="eyebrow">Geo sweep · before and after</div><h1>{E(new.dir.name)} vs {E(old.dir.name)}</h1>',
         '<p class="sub">The same Proton VPN sweep, run again after the server change. '
         '<strong>Compare hosts within a city</strong>; absolute values include the VPN hop and can change with the '
         'Proton server.</p>', f'<nav class="report-nav" aria-label="Sections">{nav}</nav>',
         f'<div class="cards cards-4">{card_html}</div>']
    for i, t, b in sections:
        h.append(f'<section id="{i}"><h2>{E(t)}</h2>{b}</section>')
    h.append('</main><footer><div class="wrap"><a class="by" href="https://zcashlabs.com"><img src="mark.svg" alt="" width="40" height="40">A Zcash Labs project</a><span>Generated by zcash-walletsync-bench geo-compare</span>'
             '<nav><a href="#top">Top</a></nav></div></footer></body></html>')
    copy_brand(out)
    (out / "compare.html").write_text("".join(h))
    return text + "\n\nwrote %s, %s, %s" % (new.dir / "compare.txt", new.dir / "compare.json", out / "compare.html")
