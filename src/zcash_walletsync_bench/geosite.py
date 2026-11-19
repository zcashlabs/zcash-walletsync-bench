"""Static page of a geo sweep: <sweep>/site/index.html, plots/*.svg, data/*.csv, site.css.

    zcash-walletsync-bench geo-site results/<sweep-dir>

It reads cities.jsonl, syncs.jsonl and the phase-2 run directories only; it never sends a
request. Same look as the run page (site.py, site_plots.py).
"""

from __future__ import annotations

import html
import json
import pathlib
import shutil

from . import site_plots as sp
from .geosweep import best_records, default_targets, latest_ok, read_jsonl, summarize_city
from .stats import ratio_ci
from .brand import copy_brand

E = html.escape
ASSETS = pathlib.Path(__file__).parent / "site_assets"
COMP = {"zecrocks": ("zec.rocks", sp.SERIES[2]), "zcashlabs": ("zcashlabs", sp.SERIES[1])}
FASTZ_COL = sp.SERIES[0]
STEPS = (("catch-up", "Catch-up"), ("first-sync", "First sync"))


def dot(c):
    return f'<span class="dot" style="--c:{c}"></span>'


def ms(v, sign=True):
    if v is None:
        return "–"
    return ("%+.1f ms" if sign else "%.1f ms") % v


class Geo:
    def __init__(self, sweep):
        self.dir = pathlib.Path(sweep)
        self.targets = default_targets()
        meta_p = self.dir / "sweep.json"
        self.meta = json.loads(meta_p.read_text()) if meta_p.exists() else {"sessions": [{}]}
        opts = (self.meta["sessions"][-1].get("options") or {}) if self.meta.get("sessions") else {}
        self.margin = float(opts.get("margin_ms", 1.0))
        self.planned = max((len(s.get("cities") or []) for s in self.meta.get("sessions", [])), default=0)
        all_recs = read_jsonl(self.dir / "cities.jsonl")
        self.recs = best_records(all_recs)
        self.failed = {}
        for r in all_recs:
            if not r.get("ok") and r["city"] not in self.recs:
                self.failed.setdefault(r["city"], []).append("%s: %s" % (r.get("server"), r.get("error")))
        self.rows = []
        for key, r in self.recs.items():
            s = summarize_city(r, self.targets, self.margin)
            if s.get("ok"):
                self.rows.append({"key": key, "rec": r, "s": s, "name": "%s, %s" % (r.get("name"), r.get("country"))})
        self.syncs = []
        for key, sy in latest_ok(read_jsonl(self.dir / "syncs.jsonl")).items():
            run = self.dir / sy["run_dir"] if sy.get("run_dir") else None
            summ = json.loads((run / "summary.json").read_text()) if run and (run / "summary.json").exists() else {}
            self.syncs.append({"key": key, "sy": sy, "summary": summ, "run": run})
        self.dead_targets = {}
        for row in self.rows:
            for t, e in (row["rec"].get("warmup_errors") or {}).items():
                if not e.startswith("1 of"):
                    self.dead_targets.setdefault(t, []).append(row["name"])

    def delta(self, row, group, ref="vs_fastz"):
        c = row["s"]["competitors"].get(group)
        return (c or {}).get(ref) or {}

    def lead(self, row):
        """Strongest competitor's lead over fastz (DNS), ms (positive: competitor faster), and its group.
        The conservative end of the CI, so a noisy exit (Reykjavik) does not rank first on a wide CI."""
        best = None
        for g in COMP:
            d = self.delta(row, g)
            if d.get("n"):
                v = -(d.get("ci95_ms") or [None, d["median_ms"]])[1]
                if best is None or v > best[0]:
                    best = (v, g)
        return best or (None, None)

    def winners(self):
        return [r for r in self.rows if r["s"]["beats_fastz_dns"]]

    def misteered(self):
        out = []
        for r in self.rows:
            st = r["s"].get("steering") or {}
            if st.get("misteered"):
                out.append((r, st))
        return out

    def sync_ratio(self, sync, step_prefix, service):
        """service / fastz wall time for the first step of that scenario, with a bootstrap CI."""
        steps = (sync["summary"] or {}).get("steps") or {}
        sid = next((k for k in sorted(steps) if k.startswith(step_prefix + "/")), None)
        if not sid:
            return None
        per = steps[sid].get("per_service") or {}
        a = ((per.get(service) or {}).get("per_rep") or {}).get("wall_s") or []
        b = ((per.get("fastz") or {}).get("per_rep") or {}).get("wall_s") or []
        if not a or not b:
            return None
        r = ratio_ci(a, b)
        med = lambda xs: sorted(xs)[len(xs) // 2]  # noqa: E731
        return {"step": sid, "ratio": r.get("ratio"), "ci95": r.get("ci95"), "significant": r.get("significant"),
                "wall_fastz": med(b), "wall": med(a), "reps": (len(a), len(b)),
                "failed": {s: (per.get(s) or {}).get("reps_failed", 0) for s in (service, "fastz")},
                "stalls": {s: ((per.get(s) or {}).get("block_streams") or {}).get("stalls") or 0
                           for s in (service, "fastz")}}


# ------------------------------------------------------------------ figures

def delta_figure(g: Geo, out):
    rows = [r for r in g.rows if any(g.delta(r, c).get("n") for c in COMP)]
    if not rows:
        return None
    rows.sort(key=lambda r: -(g.lead(r)[0] if g.lead(r)[0] is not None else -1e9))
    n = len(rows)
    import matplotlib.ticker as mt
    fig, (ax,) = sp.figure("Latency difference to fastz, per city",
                           "Median of (competitor - fastz) per round, GetLatestBlock on a warm connection; "
                           "line: 95% CI. Left of 0: the competitor answers faster.",
                           size=(13, 2.2 + 0.24 * n),
                           legend_extra=[sp.plt.Line2D([0], [0], color=COMP[c][1], marker="o", lw=2,
                                                       label=COMP[c][0] + (" (best zone)" if c == "zecrocks" else ""))
                                         for c in COMP])
    rows_csv = []
    for i, r in enumerate(rows):
        y = n - 1 - i
        for k, c in enumerate(COMP):
            d = g.delta(r, c)
            if not d.get("n"):
                continue
            yy = y + (0.17 if k == 0 else -0.17)
            lo, hi = d.get("ci95_ms") or (d["median_ms"], d["median_ms"])
            ax.plot([lo, hi], [yy, yy], color=sp.mix(COMP[c][1], .55), lw=2, solid_capstyle="round", zorder=2)
            ax.plot([d["median_ms"]], [yy], "o", ms=6, color=COMP[c][1], mec=sp.BG, mew=1.5, zorder=3)
            target = r["s"]["competitors"][c]["target"]
            rows_csv.append([r["key"], r["name"], c, target, d["median_ms"], lo, hi, d.get("verdict")])
    ax.axvspan(-g.margin, g.margin, color=sp.INK, alpha=.05, lw=0)
    ax.axvline(0, color=FASTZ_COL, lw=1.5, zorder=1)
    ax.set_xscale("symlog", linthresh=10, linscale=1.0)
    ticks = [-300, -100, -30, -10, -5, 0, 5, 10, 30, 100, 300]
    ax.xaxis.set_major_locator(mt.FixedLocator(ticks))
    ax.xaxis.set_major_formatter(mt.FuncFormatter(lambda v, _p: "0" if v == 0 else "%+g" % v))
    ax.xaxis.set_minor_locator(mt.NullLocator())
    lo = min([x[5] for x in rows_csv] + [-12])
    hi = max([x[6] for x in rows_csv] + [12])
    ax.set_xlim(min(lo * 1.6, -15), max(hi * 1.6, 15))  # symlog: a factor leaves the same room at any size
    ax.set_yticks(range(n))
    ax.set_yticklabels([r["name"] for r in reversed(rows)], color=sp.INK, fontsize=8.5)
    ax.set_ylim(-0.7, n - 0.3)
    ax.grid(False, axis="y")
    ax.set_xlabel("ms (linear within ±10, logarithmic beyond)")
    ax.text(0, 1.0, "  fastz (DNS-steered) = 0", transform=ax.get_xaxis_transform(), color=FASTZ_COL,
            fontsize=8.5, va="bottom", ha="left")
    fig.subplots_adjust(left=.2, bottom=0.75 / (2.2 + 0.24 * n))
    return sp.save(fig, out, "latency-delta"), sp.write_csv(
        out, "latency-delta", ["city", "name", "competitor", "target", "median_ms", "ci_lo_ms", "ci_hi_ms", "verdict"],
        rows_csv)


def sync_figure(g: Geo, out):
    pts = []
    for sync in g.syncs:
        row = next((r for r in g.rows if r["key"] == sync["key"]), None)
        if not row:
            continue
        for svc in sync["sy"].get("services", []):
            if svc not in COMP:
                continue
            d = g.delta(row, svc)
            for scen, _label in STEPS:
                sr = g.sync_ratio(sync, scen, svc)
                if sr and sr["ratio"] and d.get("n"):
                    pts.append((scen, svc, row["name"].split(",")[0], d["median_ms"], sr, sync["sy"].get("why")))
    if not pts:
        return None
    import matplotlib.ticker as mt
    fig, axes = sp.figure("Wallet syncs where the competitor is closer",
                          "x: latency difference to fastz in that city (phase 1). y: competitor's sync time / "
                          "fastz's (median of 3); line: 95% CI. Above 1: fastz finished first.",
                          ncols=2, size=(14, 6.4),
                          legend_extra=[sp.plt.Line2D([0], [0], color=COMP[c][1], marker="o", lw=0, label=COMP[c][0])
                                        for c in COMP if any(p[1] == c for p in pts)]
                          + [sp.plt.Line2D([0], [0], color=sp.DIM, marker="o", lw=0, label="competitor closer"),
                             sp.plt.Line2D([0], [0], color=sp.DIM, marker="s", lw=0, label="control (fastz closer)")],
                          sharey=True)
    xs = [p[3] for p in pts]
    for ax, (scen, label) in zip(axes, STEPS):
        sub = sorted([p for p in pts if p[0] == scen], key=lambda p: p[3])
        for k, (_s, svc, name, dx, sr, why) in enumerate(sub):
            col = COMP[svc][1]
            if sr.get("ci95"):
                ax.plot([dx, dx], sr["ci95"], color=sp.mix(col, .55), lw=2, solid_capstyle="round", zorder=2)
            ax.plot([dx], [sr["ratio"]], "o" if why != "control" else "s", ms=7, color=col, mec=sp.BG, mew=1.5,
                    zorder=3)
            ax.annotate(name, (dx, sr["ratio"]), xytext=(6, 5 if k % 2 == 0 else -12), textcoords="offset points",
                        fontsize=8, color=sp.DIM)
        ax.axhline(1, color=FASTZ_COL, lw=1.5, zorder=1)
        ax.axvline(0, color=sp.AXIS, lw=1, zorder=1)
        ax.set_yscale("log")
        ax.yaxis.set_major_locator(mt.LogLocator(base=10, subs=(1.0, 2.0, 5.0)))
        ax.yaxis.set_minor_locator(mt.LogLocator(base=10, subs=(1.5, 3.0, 7.0)))
        ax.yaxis.set_major_formatter(mt.FuncFormatter(lambda v, _p: "%g×" % v))
        ax.yaxis.set_minor_formatter(mt.FuncFormatter(lambda v, _p: "%g×" % v))
        ax.set_xscale("symlog", linthresh=10)
        ax.set_xlim(min(min(xs) * 1.6, -15), max(max(xs) * 1.6, 15))
        ax.tick_params(labelleft=True)
        ax.xaxis.set_major_formatter(mt.FuncFormatter(lambda v, _p: "0" if v == 0 else "%+g ms" % v))
        ax.set_title("%s %s" % (label, "1 day behind" if scen == "catch-up" else "10,000 blocks"))
        ax.set_xlabel("latency difference to fastz (ms)")
    axes[0].set_ylabel("sync time / fastz")
    rows = [[p[2], p[0], p[1], p[3], p[4]["ratio"], *(p[4].get("ci95") or ["", ""]), p[4]["wall_fastz"],
             p[4]["wall"], p[5]] for p in pts]
    return sp.save(fig, out, "sync-vs-latency"), sp.write_csv(
        out, "sync-vs-latency", ["city", "scenario", "competitor", "latency_delta_ms", "time_ratio", "ci_lo", "ci_hi",
                                 "fastz_wall_s", "competitor_wall_s", "why"], rows)


# ------------------------------------------------------------------ page

def _table(head, rows, cls="stats"):
    return ('<div class="table-wrap"><table class="%s"><thead><tr>' % cls + "".join(f"<th>{h}</th>" for h in head)
            + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
            + "</tbody></table></div>")


def _dcell(d):
    if not d.get("n"):
        return "–"
    ci = d.get("ci95_ms")
    cls = {"faster": "err", "slower": "good"}.get(d.get("verdict"), "")
    txt = ms(d["median_ms"])
    return f'<span class="{cls}">{txt}</span><em>{"[%+.1f, %+.1f]" % tuple(ci) if ci else ""}</em>'


def city_rows(g: Geo, rows):
    out = []
    for r in rows:
        s, rec = r["s"], r["rec"]
        t = s["targets"]
        fz = t.get("fastz", {})
        zr = s["competitors"].get("zecrocks", {})
        st = s.get("steering") or {}
        dns = rec.get("fastz_dns_region") or "?"
        best = st.get("best_region")
        steer = E(dns) + ("" if not best or best == dns else f' <em>best: {E(best)}</em>')
        out.append([f'{E(r["name"])}<em>{E((rec.get("exit") or {}).get("colo") or "")} · '
                    f'tunnel {s["tunnel_p50_ms"]:.0f} ms</em>',
                    steer, ms(fz.get("p50_minus_tunnel"), sign=False),
                    E((zr.get("target") or "–").replace("zecrocks-", "")), _dcell(g.delta(r, "zecrocks")),
                    _dcell(g.delta(r, "zcashlabs")), "phase 2" if s["qualifies"] else ""])
    return out


CITY_HEAD = ["City<em>exit · tunnel</em>", "fastz DNS<em>region</em>", "fastz<em>minus tunnel</em>",
             "zec.rocks<em>best zone</em>", "Δ zec.rocks<em>95% CI</em>", "Δ zcashlabs<em>95% CI</em>", ""]


def build_geo_site(sweep) -> pathlib.Path:
    sp.setup()
    g = Geo(sweep)
    out = g.dir / "site"
    for sub in ("plots", "data"):
        shutil.rmtree(out / sub, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(ASSETS / "site.css", out / "site.css")
    copy_brand(out)
    sections = []

    def section(ident, title, body):
        sections.append((ident, title, "".join(body)))

    win = sorted(g.winners(), key=lambda r: -(g.lead(r)[0] or 0))
    mis = g.misteered()
    zr_deltas = sorted(g.delta(r, "zecrocks")["median_ms"] for r in g.rows if g.delta(r, "zecrocks").get("n"))
    zl_deltas = sorted(g.delta(r, "zcashlabs")["median_ms"] for r in g.rows if g.delta(r, "zcashlabs").get("n"))
    med = lambda xs: xs[len(xs) // 2] if xs else None  # noqa: E731

    # Sync verdicts: fastz faster (ratio CI above 1), slower, or not distinguishable.
    verdicts = []
    for sync in g.syncs:
        for svc in sync["sy"].get("services", []):
            if svc in COMP:
                for scen, _l in STEPS:
                    sr = g.sync_ratio(sync, scen, svc)
                    if sr and sr["ratio"]:
                        v = "fastz" if sr["significant"] and sr["ratio"] > 1 else \
                            "competitor" if sr["significant"] else "tie"
                        verdicts.append((sync, svc, scen, sr, v))
    in_win = [v for v in verdicts if v[0]["sy"].get("why") != "control"]

    lead_v, lead_g = g.lead(win[0]) if win else (None, None)
    cards = [
        ("Cities measured", "%d" % len(g.rows), "of %d planned · %d failed" % (g.planned or len(g.rows), len(g.failed))),
        ("Competitor closer than fastz", "%d" % len(win),
         ", ".join(r["name"].split(",")[0] for r in win[:4]) + (" …" if len(win) > 4 else "") or "none"),
        ("Largest competitor lead", ms(lead_v, sign=False) if lead_v else "–",
         "at least, 95%% CI: %s, %s" % (win[0]["name"].split(",")[0], win[0]["s"]["competitors"][lead_g]["target"])
         if win else ""),
        ("Median Δ zec.rocks", ms(med(zr_deltas)), "competitor minus fastz, over all cities"),
        ("DNS chose a slower region", "%d" % len(mis), "cities; see DNS steering"),
        ("Syncs fastz won there", "%d of %d" % (sum(v[4] == "fastz" for v in in_win), len(in_win)) if in_win else "–",
         "tasks in cities where a competitor is closer; %d ties" % sum(v[4] == "tie" for v in in_win)
         if in_win else "phase 2 not run yet"),
    ]
    card_html = "".join(f'<div class="card"><span class="who">{E(a)}</span><b>{E(b)}</b><span>{E(c)}</span></div>'
                        for a, b, c in cards)

    # Latency
    body = ["<p>In every city each target got one warm <code>GetLatestBlock</code> per round, 24 rounds, in a new "
            "random order each round. The number that matters is the difference to fastz in the same city: the trip "
            "from this machine to the VPN server is in every measurement and cancels out. fastz here is "
            "<code>rpc.fastz.cash</code> as DNS steers it from that city; zec.rocks is its best zone there (chosen on "
            "half the rounds, compared on the other half).</p>"]
    made = delta_figure(g, out)
    if made:
        body.append(f'<figure><img loading="lazy" alt="Latency difference to fastz per city" src="{made[0]}">'
                    f'<figcaption class="downloads"><a href="{made[1]}">Data (CSV)</a> · <a href="{made[0]}">SVG</a>'
                    '</figcaption></figure>')
    if win:
        body.append('<details><summary class="meta">%d cities where a competitor answers faster, by its lead'
                    '</summary>%s</details>' % (len(win), _table(CITY_HEAD, city_rows(g, win))))
    else:
        body.append('<p class="note">No city had a competitor faster than fastz beyond the CI.</p>')
    rest = sorted([r for r in g.rows if r not in win], key=lambda r: r["name"])
    body.append('<details><summary class="meta">All %d cities</summary>%s</details>'
                % (len(g.rows), _table(CITY_HEAD, city_rows(g, sorted(g.rows, key=lambda r: r["name"])))))
    _ = rest
    section("latency", "Latency", body)

    # DNS steering
    body = []
    items = []
    if mis:
        for r, st in sorted(mis, key=lambda x: -x[1]["dns_vs_best"]["median_ms"]):
            items.append(f'<strong>{E(r["name"])}</strong>: DNS gave <code>{E(st["dns_region"] or "?")}</code>, '
                         f'<code>{E(st["best_region"])}</code> answers {st["dns_vs_best"]["median_ms"]:.1f} ms faster')
    else:
        items.append("In every city DNS chose the fastest fastz region, or one within the CI of it.")
    off = [r for r in g.rows if (r["rec"].get("resolver") or {}).get("same_net_as_exit") is False]
    unk = [r for r in g.rows if (r["rec"].get("resolver") or {}).get("same_net_as_exit") is None]
    items.append("Proton's resolver asked from the exit's own network in %d cities%s%s." % (
        len(g.rows) - len(off) - len(unk),
        "; from another network in " + ", ".join(E(r["name"]) for r in off) if off else "",
        "; unknown in %d" % len(unk) if unk else ""))
    regions = {}
    for r in g.rows:
        regions.setdefault(r["rec"].get("fastz_dns_region") or "?", []).append(r["name"].split(",")[0])
    items += [f'<code>{E(k)}</code> served {len(v)}: {E(", ".join(sorted(v)))}' for k, v in sorted(regions.items())]
    body.append('<ul class="bullets">' + "".join(f"<li>{x}</li>" for x in items) + "</ul>")
    section("dns", "DNS steering", body)

    # Syncs
    body = []
    made = sync_figure(g, out)
    if made:
        body.append("<p>In the cities where a competitor was closer, plus control cities where fastz was "
                    "narrowly closer, each service ran the Vizor wallet's catch-up (1 day behind) and first sync "
                    "(10,000 blocks), 3 repetitions, order rotated, every name pinned to the address Proton's "
                    "resolver gave.</p>")
        body.append(f'<figure><img loading="lazy" alt="Sync time ratio against latency difference" src="{made[0]}">'
                    f'<figcaption class="downloads"><a href="{made[1]}">Data (CSV)</a> · <a href="{made[0]}">SVG</a>'
                    '</figcaption></figure>')
        rows = []
        for sync, svc, scen, sr, v in verdicts:
            row = next((r for r in g.rows if r["key"] == sync["key"]), None)
            name = row["name"] if row else sync["key"]
            link = f'<a href="../{E(str(sync["sy"]["run_dir"]))}/site/index.html">run</a>' if sync["sy"].get(
                "run_dir") else ""
            ci = sr.get("ci95")
            cls = {"fastz": "good", "competitor": "err"}.get(v, "")
            fails = ", ".join("%s %d" % (k, n) for k, n in sr["failed"].items() if n)
            stalls = "%d / %d" % (sr["stalls"]["fastz"], sr["stalls"][svc])
            rows.append([f'{E(name)}<em>{E(sync["sy"].get("why", ""))}</em>',
                         f'{dot(COMP[svc][1])}{E(COMP[svc][0])}', E(sr["step"]),
                         ms(g.delta(row, svc).get("median_ms")) if row else "–",
                         sp.fmt_s(sr["wall_fastz"]), sp.fmt_s(sr["wall"]),
                         f'<span class="{cls}">{sr["ratio"]:.2f}×</span><em>'
                         f'{"[%.2f, %.2f]" % tuple(ci) if ci else ""}</em>',
                         stalls, (E("failed reps: " + fails) if fails else "") + " " + link])
        body.append(_table(["City", "Competitor", "Task", "Δ latency", "fastz", "competitor",
                            "Time ratio<em>competitor / fastz</em>", "Stalls<em>fastz / competitor</em>", ""],
                           rows))
        body.append('<p class="note">Times are medians of 3 repetitions. A stall is a gap of more than 2 s '
                    'between two messages of a block stream. Several cities show fastz streams stalling '
                    'while the competitor, through the same tunnel, does not.</p>')
    else:
        body.append('<p class="note">Phase 2 has not run, or none of its runs finished.</p>')
    section("syncs", "Wallet syncs", body)

    # Failures
    body = []
    if g.failed:
        body.append(_table(["City", "Attempts"], [[E(k), "<br>".join(E(x) for x in v)] for k, v in sorted(
            g.failed.items())]))
    for t, cities in sorted(g.dead_targets.items()):
        body.append(f"<p><code>{E(t)}</code> did not answer its warm-up calls in {len(cities)} of {len(g.rows)} "
                    f"cities and was not measured there.</p>")
    if not body:
        body.append("<p>Nothing failed.</p>")
    section("failures", "Failures", body)

    # Method
    m_items = [
        "<strong>Only differences within one city mean anything.</strong> All traffic went through Proton VPN from "
        "one machine, so every number includes the trip from that machine to the VPN server (the tunnel "
        "baseline, a DNS query to Proton's resolver, timed each round). Latency minus the tunnel baseline "
        "approximates what someone in that city would see, but tunnel jitter and Proton's own routing remain in it.",
        "Only Proton servers that are physically in the city were used: smart-routed locations, Secure Core and "
        "Tor servers were left out, and cities closer than 150 km to one already kept were dropped. The exit "
        "country was checked with Cloudflare's trace in every city.",
        "Names were resolved through Proton's resolver inside the tunnel, so <code>rpc.fastz.cash</code> was "
        "steered by where that resolver is, as it would be for a user there with that resolver. Everything ran "
        "over IPv4 (fastz has no IPv6 address).",
        "Latency is a single cheap RPC (GetLatestBlock) on a connection that was already open; it shows the "
        "network path and the front end, not the work behind heavier calls. <strong>zec.rocks answers it from a "
        "cache layer on Fly</strong> (headers <code>x-cache: hit</code>, <code>x-backend: cache</code>) in about 14 "
        "places, while its block streams come from 5 backend regions; so its latency here is the distance to the "
        "nearest cache machine. The syncs measure the rest.",
        "Differences are paired by round: each round called every target once within a few seconds, so slow "
        "drift in the tunnel hits all targets alike. CIs are bootstrap 95%% intervals of the median; a difference "
        "inside ±%g ms or a CI that crosses it is a tie." % g.margin,
        "Phase-2 runs are 3 repetitions per task, so their CIs are wide; a tie there means not distinguishable "
        "with that few runs, not equal.",
    ]
    section("method", "Method and caveats", ['<ul class="bullets">' + "".join(f"<li>{x}</li>" for x in m_items)
                                             + "</ul>"])
    section("data", "Data", ['<ul class="bullets"><li><a href="../cities.jsonl">cities.jsonl</a>: every round '
                             'of every city</li><li><a href="../syncs.jsonl">syncs.jsonl</a> and '
                             '<code>runs/</code>: phase-2 runs, each with its own page</li>'
                             '<li><a href="../summary.json">summary.json</a>: the table of '
                             '<code>zcash-walletsync-bench geo-report</code></li></ul>'])

    nav = "".join(f'<a href="#{i}">{E(t)}</a>' for i, t, _ in sections)
    title = "fastz vs zec.rocks and zcashlabs from %d cities" % len(g.rows)
    sub = ("Latency from cities around the world through Proton VPN, and wallet syncs in the cities where a "
           "competitor is closer. <strong>Compare hosts within a city</strong>; absolute values include the VPN hop.")
    h = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width, initial-scale=1">',
         f"<title>Geo sweep · zcash-walletsync-bench</title>", '<meta name="theme-color" content="#171415">',
         '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" '
         'href="https://fonts.gstatic.com" crossorigin>',
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Sora:wght@700;800&family=Manrope:'
         'wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">',
         '<link rel="icon" href="favicon.svg" type="image/svg+xml"><link rel="stylesheet" href="site.css"></head><body class="benchmarks-page">',
         f'<header><div class="wrap"><a class="brand" href="#top"><img src="mark.svg" alt="" width="40" height="40"><b>zcash-walletsync-bench</b><span>{E(g.dir.name)}</span>'
         '</a><nav>' + ('<a href="compare.html">Before/after</a>' if (out / "compare.html").exists() else '') + '<a href="map.html">Region map</a><a href="#latency">Latency</a><a href="#syncs">Syncs</a><a href="#data">Data</a></nav></div></header>',
         '<main class="wrap benchmark-report" id="top">',
         f'<div class="eyebrow">Geo sweep · Proton VPN</div><h1>{E(title)}</h1><p class="sub">{sub}</p>',
         f'<nav class="report-nav" aria-label="Sections">{nav}</nav>',
         f'<div class="cards cards-6">{card_html}</div>']
    for i, t, b in sections:
        h.append(f'<section id="{i}"><h2>{E(t)}</h2>{b}</section>')
    h.append('</main><footer><div class="wrap"><a class="by" href="https://zcashlabs.com"><img src="mark.svg" alt="" width="40" height="40">A Zcash Labs project</a><span>Generated by zcash-walletsync-bench from cities.jsonl and runs/</span>'
             '<nav><a href="#top">Top</a></nav></div></footer></body></html>')
    p = out / "index.html"
    p.write_text("".join(h))
    return p
