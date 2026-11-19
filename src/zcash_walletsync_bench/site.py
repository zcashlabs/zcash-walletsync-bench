"""Static site page of one run: <run>/site/index.html, plots/*.svg, data/*.csv, site.css.

    zcash-walletsync-bench site results/<run-dir>

It reads run.json, summary.json and raw/ only; it never sends a request. The page puts the
results first (a scoreboard, then one figure per question) and keeps the full statistics of
each step behind a step picker. report.md keeps every table.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import math
import pathlib
import shutil
from collections import Counter, defaultdict

from . import rawio
from . import site_plots as sp
from .brand import copy_brand

ASSETS = pathlib.Path(__file__).parent / "site_assets"
PROFILE = {"vizor": "Vizor", "zingo": "Zingo", "librustzcash": "librustzcash"}
BEHIND = {"1h": "1 hour", "1d": "1 day", "1w": "1 week"}
SCEN_ORDER = ["first-sync", "catch-up", "enhance", "transparent", "mixed"]
LAT_ORDER = ["GetLatestBlock", "GetLightdInfo", "GetTreeState", "GetSubtreeRoots", "GetAddressUtxosStream",
             "GetTaddressTxids", "GetTransaction"]
METRIC_LABEL = {
    "wall_s": "Wall time", "time_to_first_block_s": "First block", "mbytes_per_s": "MB/s",
    "blocks_per_s": "Blocks/s", "prelude_s": "Before first block request", "blocks_phase_s": "Block download",
    "tail_s": "After last block", "stream_resumes": "Stream re-opens", "tx_per_s": "Transactions/s",
    "utxo_s": "UTXO refresh", "history_s": "Tx history", "full_history_s": "Full-history search",
    "first_rpc_ms": "First RPC", "grpc_connect_ms": "gRPC connect", "aggregate_mbytes_per_s": "All wallets MB/s",
    "wallet_mbytes_per_s": "Per-wallet MB/s", "wallet_wall_s": "Per-wallet session",
    "wallet_wall_p50_s": "Per-wallet session p50", "wallet_wall_max_s": "Slowest wallet session",
    "block_arrival_delay_s": "New-block delay", "client_parse_s": "Client parse time", "connect_ms": "Connect",
    "polls": "Polls", "poll_errors": "Poll errors", "mempool_txs_seen": "Mempool txs seen",
    "mempool_stream_opens": "Mempool stream opens", "aggregate_blocks_per_s": "All wallets blocks/s",
}
HIGHER_BETTER = {"mbytes_per_s", "blocks_per_s", "tx_per_s", "aggregate_mbytes_per_s", "wallet_mbytes_per_s",
                 "aggregate_blocks_per_s", "polls", "mempool_txs_seen"}
E = html.escape


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and not math.isfinite(v))


def fmt(v, unit: str) -> str:
    if not ok(v):
        return "–"
    if unit == "s":
        return sp.fmt_s(v)
    if unit == "ms":
        return sp.fmt_s(v / 1000.0)
    if unit in ("MB/s", "blocks/s", "tx/s"):
        if v >= 1000:
            return f"{v:,.0f} {unit}"
        return f"{v:.3g} {unit}" if v < 100 else f"{v:.0f} {unit}"
    if unit == "count":
        return f"{v:,.0f}" if float(v).is_integer() else f"{v:.2f}"
    return f"{v:.3g}"


def ratio_txt(r) -> str:
    if not ok(r):
        return "–"
    return f"{r:.1f}×" if r < 100 else f"{r:,.0f}×"


def num(v) -> str:
    return f"{v:,}" if isinstance(v, int) else f"{v:,.0f}"


def dot(c: str) -> str:
    return f'<i class="dot" style="--c:{c}"></i>'


def unit_of(metric: str) -> str:
    if "mbytes_per_s" in metric:
        return "MB/s"
    if "blocks_per_s" in metric:
        return "blocks/s"
    if metric == "tx_per_s":
        return "tx/s"
    if metric.endswith("_ms"):
        return "ms"
    if metric.endswith("_s"):
        return "s"
    return "count"


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

class Model:
    def __init__(self, run_dir):
        self.run_dir = pathlib.Path(run_dir)
        self.run = json.loads((self.run_dir / "run.json").read_text())
        self.summary = json.loads((self.run_dir / "summary.json").read_text())
        self.calls = rawio.read_jsonl(self.run_dir, "calls")
        self.tips_raw = rawio.read_jsonl(self.run_dir, "tips")
        self.incomplete = self._incomplete(rawio.read_jsonl(self.run_dir, "runs", stress=False))
        self.services = list(self.summary["services"])
        self.col = sp.colors(self.services)
        self.svc = {s["name"]: s for s in self.run.get("services", [])}
        self.steps = self.summary.get("steps") or {}
        self.opts = self.run.get("options") or {}
        self.tasks = self._tasks()
        self._latency()
        self.scale = None
        self.scale_behind = self.opts.get("scale_behind", "")
        for prof, per in (self.summary.get("scale") or {}).items():
            self.scale = per
            self.scale_profile = prof
            break
        self._tips()

    @staticmethod
    def _incomplete(runs) -> dict:
        """(step, service, metric or '*') -> repetitions whose result says part of the work failed.

        A result field X_ok that is false (or a list holding false) marks the metric X_s; ok or
        all_ok false, or a repetition error, marks every metric of the step."""
        out = Counter()
        for r in runs:
            res = r.get("result") if isinstance(r.get("result"), dict) else {}
            bad = set()
            if r.get("error"):
                bad.add("*")
            for k, v in res.items():
                if not (k == "ok" or k.endswith("_ok")):
                    continue
                failed = (v is False or v == 0) if not isinstance(v, list) else any(x is False or x == 0 for x in v)
                if failed:
                    bad.add("*" if k in ("ok", "all_ok") else k[:-3] + "_s")
            for b in bad:
                out[(r.get("step"), r.get("service"), b)] += 1
        return out

    def failed_reps(self, t, s) -> int:
        return self.incomplete.get((t["step"], s, t["metric"]), 0) + self.incomplete.get((t["step"], s, "*"), 0)

    # --- wallet tasks: one comparable metric of one step ---
    def _task(self, sid, metric, label, phases=False, phrase=None):
        st = self.steps[sid]
        reps, stats = {}, {}
        for s in self.services:
            ps = st["per_service"].get(s) or {}
            reps[s] = [x for x in (ps.get("per_rep") or {}).get(metric, []) if x is not None]
            stats[s] = (ps.get("rep_stats") or {}).get(metric) or {}
        if not any(reps.values()):
            return None
        t = dict(id=f"{sid}:{metric}", step=sid, metric=metric, label=label, phrase=phrase or label,
                 unit=unit_of(metric),
                 lower=metric not in HIGHER_BETTER, reps=reps, stats=stats,
                 compare=(st.get("compare") or {}).get(metric), scenario=st["scenario"])
        t["failed"] = {s: self.failed_reps(t, s) for s in self.services}
        if phases:
            t["phases"] = {}
            for s in self.services:
                rs = (st["per_service"].get(s) or {}).get("rep_stats") or {}
                if rs.get("prelude_s", {}).get("n"):
                    t["phases"][s] = {k: (rs.get(k) or {}).get("p50") for k in ("prelude_s", "blocks_phase_s", "tail_s")}
        return t

    def _tasks(self):
        out = []
        order = {k: i for i, k in enumerate(SCEN_ORDER)}

        def key(sid):
            st = self.steps[sid]
            p = st.get("params") or {}
            prof = list(PROFILE).index(p.get("profile")) if p.get("profile") in PROFILE else 9
            size = p.get("blocks") or {"1h": 1, "1d": 2, "1w": 3}.get(p.get("behind_one"), 0)
            return (order.get(st["scenario"], 99), prof, size)

        for sid in sorted((s for s in self.steps if self.steps[s]["scenario"] in order), key=key):
            st = self.steps[sid]
            p = st.get("params") or {}
            prof = PROFILE.get(p.get("profile"), p.get("profile") or "")
            sc = st["scenario"]
            if sc == "first-sync":
                t = self._task(sid, "wall_s", f"First sync · {p.get('blocks', 0):,} blocks · {prof}", phases=True,
                               phrase=f"{prof} first sync of {p.get('blocks', 0):,} blocks")
            elif sc == "catch-up":
                b = BEHIND.get(p.get("behind_one"), p.get("behind_one"))
                t = self._task(sid, "wall_s", f"Catch-up after {b} away · {prof}", phases=True,
                               phrase=f"{prof} catch-up after {b} away")
            elif sc == "enhance":
                n = self._first_rep(sid, "txids")
                t = self._task(sid, "wall_s", f"Enhance {n or ''} transactions · {prof}".replace("  ", " "))
            elif sc == "transparent":
                for m_, lab in (("utxo_s", "UTXO refresh"), ("history_s", "Tx history of known addresses"),
                                ("full_history_s", "Full-history search")):
                    t = self._task(sid, m_, f"{lab} · {prof}")
                    if t:
                        out.append(t)
                continue
            elif sc == "mixed":
                n = self._first_rep(sid, "wallets")
                t = self._task(sid, "wallet_wall_s", f"Mixed, {n or ''} wallets: one session · {prof}")
            else:
                continue
            if t:
                out.append(t)
        if "app-open" in self.steps:
            for m_, lab in (("grpc_connect_ms", "App open · gRPC connect"), ("first_rpc_ms", "App open · first RPC")):
                t = self._task("app-open", m_, lab)
                if t:
                    out.append(t)
        return out

    def _first_rep(self, sid, k):
        for ps in self.steps[sid]["per_service"].values():
            v = (ps.get("per_rep") or {}).get(k)
            if v:
                return int(v[0])
        return None

    # --- per-RPC latency, warm calls of every step ---
    def _latency(self):
        lat = defaultdict(list)
        for c in self.calls:
            if not c.get("step") or not c.get("ok") or c.get("cold") or c.get("scenario") == "stress":
                continue
            if c["method"] == "GetBlockRange":
                if c.get("ttfb_ms") is not None:
                    lat[("GetBlockRange:first", c["service"])].append(c["ttfb_ms"])
            elif c.get("latency_ms") is not None:
                lat[(c["method"], c["service"])].append(c["latency_ms"])
        self.latency = lat
        counts = Counter()
        for (k, _s), v in lat.items():
            counts[k] += len(v)
        meths = [m for m in LAT_ORDER if counts[m] >= 20]
        meths += sorted(m for m in counts if m not in LAT_ORDER and m != "GetBlockRange:first"
                        and m != "GetMempoolStream" and counts[m] >= 20)
        self.latency_panels = [(m, m) for m in meths]
        if counts["GetBlockRange:first"] >= 20:
            self.latency_panels.append(("GetBlockRange · first block", "GetBlockRange:first"))
        rtts = [(s.get("connection") or {}).get("tcp_connect_ms_min") for s in self.svc.values()]
        rtts = [r for r in rtts if r]
        self.rtt_ms = min(rtts) if rtts else None

    # --- tips ---
    def _tips(self):
        lag = defaultdict(list)
        for t in self.tips_raw:
            for s in self.services:
                v = (t.get("lag_blocks") or {}).get(s)
                if v is not None:
                    lag[s].append(v)
        self.tip_lag = dict(lag)
        obs = float(self.opts.get("observe_s") or 3.0)
        self.observe_label = f"{obs:g} s"
        reg = [t for t in self.tips_raw if t.get("label") in ("tip-follow", "soak")]
        first_seen = {s: {} for s in self.services}
        for t in reg:
            for s in self.services:
                h = (t.get("heights") or {}).get(s)
                if h is not None:
                    first_seen[s].setdefault(h, t["at_s"])
        arr = {s: [] for s in self.services}
        if reg and all(first_seen.values()):
            floor = max(min(v) for v in first_seen.values())
            common = set.intersection(*(set(v) for v in first_seen.values()))
            for h in sorted(common):
                if h <= floor:
                    continue
                first = min(first_seen[s][h] for s in self.services)
                for s in self.services:
                    arr[s].append(round((first_seen[s][h] - first) / obs))
        self.tip_arrival = arr

    # --- helpers ---
    def median(self, t, s):
        return (t["stats"].get(s) or {}).get("p50")

    def best_of(self, t):
        meds = {s: self.median(t, s) for s in self.services if ok(self.median(t, s))}
        if not meds:
            return None, {}
        c = t.get("compare") or {}
        best = c.get("best") if c.get("best") in meds else (min if t["lower"] else max)(meds, key=meds.get)
        return best, meds

    def verdict(self, t, s):
        """'best', 'tie' (95% CI of the ratio to the best contains 1), 'worse', or None."""
        best, meds = self.best_of(t)
        if s not in meds:
            return None, None
        if s == best:
            return "best", 1.0
        vs = ((t.get("compare") or {}).get("vs_best") or {}).get(s) or {}
        r = vs.get("ratio")
        if r is None:
            r = meds[s] / meds[best] if meds[best] else None
        if r is not None and not t["lower"] and r:
            r = 1 / r  # always "times worse"
        if vs.get("ci95") and not vs.get("significant"):
            return "tie", r
        return "worse", r


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------

class Page:
    def __init__(self):
        self.sections = []

    def section(self, ident, title, nav=None):
        self.sections.append(dict(id=ident, title=title, nav=nav or title, body=[]))

    def add(self, h):
        self.sections[-1]["body"].append(h)

    def p(self, h):
        self.add(f"<p>{h}</p>")

    def note(self, h):
        self.add(f'<p class="note">{h}</p>')

    def bullets(self, items):
        self.add('<ul class="bullets">' + "".join(f"<li>{x}</li>" for x in items) + "</ul>")

    def fig(self, made, alt):
        if not made:
            return
        svg, data = made
        self.add(f'<figure><img loading="lazy" alt="{E(alt)}" src="{svg}"><figcaption class="downloads">'
                 f'<a href="{data}">Data (CSV)</a> · <a href="{svg}">SVG</a></figcaption></figure>')

    def table(self, head, rows, cls=""):
        self.add(f'<div class="table-wrap"><table class="{cls}"><thead><tr>' + "".join(f"<th>{x}</th>" for x in head)
                 + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
                 + "</tbody></table></div>")


def svc_label(m, s):
    return f'{dot(m.col[s])}<span class="svc">{E(s)}</span>'


def scoreboard(m: Model, rows) -> str:
    head = "<th>Task</th>" + "".join(f"<th>{svc_label(m, s)}</th>" for s in m.services)
    body = []
    for t in rows:
        best, meds = m.best_of(t)
        if not meds:
            continue
        top = max(meds.values())
        cells = []
        for s in m.services:
            v = meds.get(s)
            if v is None:
                cells.append("<td>–</td>")
                continue
            kind, r = m.verdict(t, s)
            tag = {"best": "best", "tie": "≈ best", "worse": ratio_txt(r) + (" slower" if t["lower"] else " less")}[kind]
            title = {"best": "best median",
                     "tie": "the 95% CI of the ratio to the best contains 1: this run cannot tell them apart",
                     "worse": "median / best median; the 95% CI excludes 1"}[kind]
            w = max(2.0, 100 * v / top) if top else 0
            nf = (t.get("failed") or {}).get(s)
            warn = (f'<span class="warn" title="part of the work failed in these repetitions, so the time is not '
                    f'comparable">⚠ incomplete in {nf} of {len(t["reps"].get(s, []))}</span>') if nf else ""
            cells.append(f'<td class="sc {kind}" title="{E(title)}"><span class="v">{E(fmt(v, t["unit"]))}</span>'
                         f'<span class="r">{E(tag)}</span>{warn}<i class="bar" style="--w:{w:.1f};--c:{m.col[s]}"></i></td>')
        arrow = "↓" if t["lower"] else "↑"
        body.append(f'<tr><th scope="row">{E(t["label"])} <em>{arrow}</em></th>{"".join(cells)}</tr>')
    return (f'<div class="table-wrap"><table class="scoreboard"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def scale_rows(m: Model):
    """Scoreboard rows of the highest scale level."""
    if not m.scale:
        return []
    levels = sorted({int(k) for per in m.scale.values() for k in per})
    top = levels[-1]
    sid = next((k for k, v in m.steps.items() if v["scenario"] == "scale"
                and (v.get("params") or {}).get("scale_wallets") == top), None)
    if not sid:
        return []
    t = m._task(sid, "aggregate_mbytes_per_s", f"{top} wallets at once · all wallets MB/s")
    return [t] if t else []


def cards(m: Model):
    out = []

    def task_card(t, title):
        best, meds = m.best_of(t)
        if not best:
            return
        others = " · ".join(f'{E(s)} {E(fmt(meds[s], t["unit"]))}' for s in sorted(meds, key=meds.get,
                            reverse=not t["lower"]) if s != best)
        kind = "fastest" if t["lower"] else "highest"
        ties = [s for s in meds if s != best and m.verdict(t, s)[0] == "tie"]
        tie = f" (tied with {', '.join(ties)})" if ties else ""
        out.append(f'<div class="card">{E(title)}<b>{E(fmt(meds[best], t["unit"]))}</b>'
                   f'<span class="who">{dot(m.col[best])}{E(best)} {kind}{E(tie)}</span><span>{others}</span></div>')

    by_step = {t["step"]: t for t in m.tasks if t["metric"] == "wall_s"}
    fs = [t for t in m.tasks if t["scenario"] == "first-sync"]
    pick = by_step.get("first-sync/vizor/10000") or (fs[-1] if fs else None)
    if pick:
        task_card(pick, pick["label"])
    cu = [t for t in m.tasks if t["scenario"] == "catch-up"]
    if cu:
        task_card(cu[-1], cu[-1]["label"])
    ao = next((t for t in m.tasks if t["metric"] == "first_rpc_ms"), None)
    if ao:
        task_card(ao, "App open, first RPC")
    sr = scale_rows(m)
    if sr:
        task_card(sr[0], sr[0]["label"])
    if m.tip_lag:
        share = {s: sum(1 for x in v if x == 0) / len(v) for s, v in m.tip_lag.items() if v}
        best = max(share, key=share.get)
        others = " · ".join(f"{E(s)} {share[s] * 100:.0f}%" for s in sorted(share, key=share.get, reverse=True) if s != best)
        out.append(f'<div class="card">At the tip when asked<b>{share[best] * 100:.0f}%</b>'
                   f'<span class="who">{dot(m.col[best])}{E(best)}</span><span>{others}</span></div>')
    cc = correctness(m)
    if cc["steps"]:
        out.append(f'<div class="card">Answers compared across services<b>{cc["mismatches"]:,} mismatch'
                   f'{"" if cc["mismatches"] == 1 else "es"}</b><span class="who">{cc["compared"]:,} items</span>'
                   f'<span>{E(cc["what"])}</span></div>')
    return out[:6]


def correctness(m: Model) -> dict:
    kinds = defaultdict(lambda: [0, 0, 0])
    odd = Counter()
    orders = {}
    unstable = []
    steps = 0
    for sid, st in m.steps.items():
        cc = st.get("crosscheck")
        if not cc:
            continue
        steps += 1
        for k, v in (cc.get("kinds") or {}).items():
            kinds[k][0] += v.get("compared") or 0
            kinds[k][1] += v.get("mismatches") or 0
            kinds[k][2] += v.get("aux_differences") or 0
            if v.get("completing_hash_order"):
                orders = v["completing_hash_order"]
        for s, n in (cc.get("odd_one_out") or {}).items():
            odd[s] += n if isinstance(n, int) else 1
        for u in cc.get("unstable_between_reps") or []:
            unstable.append((sid, u))
    names = {"block": "blocks", "tree_state": "tree states", "subtree_roots": "subtree-root lists", "tx": "transactions",
             "taddr_txids": "address tx lists", "utxos": "UTXO sets"}
    what = ", ".join(f"{v[0]:,} {names.get(k, k)}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1][0]))
    return dict(kinds=dict(kinds), mismatches=sum(v[1] for v in kinds.values()),
                compared=sum(v[0] for v in kinds.values()), aux=sum(v[2] for v in kinds.values()), odd=odd,
                orders=orders, unstable=unstable, steps=steps, names=names, what=what)


def findings(m: Model) -> list[str]:
    out = []
    rows = [t for t in m.tasks] + scale_rows(m)
    tally = Counter()
    n = 0
    for t in rows:
        best, meds = m.best_of(t)
        if not best:
            continue
        n += 1
        for s in meds:
            if m.verdict(t, s)[0] in ("best", "tie"):
                tally[s] += 1
    if n:
        out.append("<strong>Best or tied</strong> (95% CI of the ratio contains 1) in " + ", ".join(
            f"{E(s)} {tally[s]}" for s in sorted(m.services, key=lambda s: -tally[s])) + f" of {n} tasks.")

    # Where a first sync loses its time.
    fs = next((t for t in m.tasks if t["step"] == "first-sync/vizor/10000"), None) or \
        next((t for t in m.tasks if t.get("phases")), None)
    if fs and fs.get("phases"):
        pre = {s: v["prelude_s"] for s, v in fs["phases"].items() if v.get("prelude_s") is not None}
        if pre:
            worst = max(pre, key=pre.get)
            wall = m.median(fs, worst)
            others = ", ".join(f"{E(s)} {fmt(pre[s], 's')}" for s in sorted(pre, key=pre.get) if s != worst)
            txt = (f"<strong>Before the first block</strong>: in a {E(fs['phrase'])}, {E(worst)} spends "
                   f"{fmt(pre[worst], 's')} before its first block request")
            if wall:
                txt += f" ({pre[worst] / wall * 100:.0f}% of its {fmt(wall, 's')})"
            txt += f"; {others}."
            comp = m.steps[fs["step"]].get("compare") or {}
            cand = []
            for k, c in comp.items():
                if not k.endswith(" latency_ms") or k.startswith("GetBlockRange") or not c:
                    continue
                r = (c.get("vs_best") or {}).get(worst) or {}
                if r.get("significant") and r.get("ratio"):
                    cand.append((r["ratio"], k.split()[0], c["best"]))
            if cand:
                r, meth, best = max(cand)
                ps = m.steps[fs["step"]]["per_service"]
                a = ((ps[worst]["methods"].get(meth) or {}).get("latency_ms") or {}).get("p50")
                b = ((ps[best]["methods"].get(meth) or {}).get("latency_ms") or {}).get("p50")
                txt += (f" The largest gap is <code>{meth}</code>: median {fmt(a, 'ms')} on {E(worst)}, "
                        f"{fmt(b, 'ms')} on {E(best)} ({ratio_txt(r)}).")
            out.append(txt)
        blk = {s: v["blocks_phase_s"] for s, v in fs["phases"].items() if v.get("blocks_phase_s")}
        if len(blk) > 1:
            lo, hi = min(blk.values()), max(blk.values())
            out.append(f"<strong>Block download</strong> itself is closer: {fmt(lo, 's')} to {fmt(hi, 's')} "
                       f"({ratio_txt(hi / lo)}) in the same step, once the first block is requested.")

    if m.scale:
        levels = sorted({int(k) for per in m.scale.values() for k in per})
        a, b = str(levels[0]), str(levels[-1])
        parts = []
        for s in m.services:
            per = m.scale.get(s) or {}
            if a in per and b in per:
                x = per[a]["aggregate_mbytes_per_s"].get("p50")
                y = per[b]["aggregate_mbytes_per_s"].get("p50")
                if x and y:
                    parts.append(f"{E(s)} {x:.3g} → {y:.3g} MB/s ({y / x:.1f}×)")
        errs = sum(e.get("errors", 0) for per in m.scale.values() for e in per.values())
        if parts:
            out.append(f"<strong>From {a} to {b} wallets at once</strong>, the total download rate went " +
                       "; ".join(parts) + (". No call failed." if not errs else f". {errs:,} calls failed."))

    if m.tip_lag:
        parts = []
        for s in m.services:
            v = m.tip_lag.get(s) or []
            if v:
                parts.append(f"{E(s)} {sum(1 for x in v if x > 0) / len(v) * 100:.0f}% (max {max(v):g})")
        out.append("<strong>Behind the highest tip</strong> in this share of snapshots: " + ", ".join(parts) + ".")

    cc = correctness(m)
    if cc["steps"]:
        txt = (f"<strong>Correctness</strong>: {cc['compared']:,} items compared across services, "
               f"{cc['mismatches']:,} mismatched.")
        if cc["orders"] and len(set(cc["orders"].values())) > 1:
            cnt = Counter(cc["orders"].values())
            minority = [s for s, o in cc["orders"].items() if cnt[o] < max(cnt.values())]
            if minority:
                txt += (f" {', '.join(E(s) for s in minority)} returns the subtree-root <code>completingBlockHash</code> "
                        f"in {E(cc['orders'][minority[0]])} byte order, the others in "
                        f"{E(next(o for o in cc['orders'].values() if cnt[o] == max(cnt.values())))} order. "
                        "Wallets do not read that field, so it counts as an aux-only difference.")
        out.append(txt)
    err = errors_total(m)
    tot = sum(v["errors"] for v in err.values())
    dl = sum(v["deadline"] for v in err.values())
    txt = (f"<strong>Errors</strong>: {tot:,} of {sum(v['calls'] for v in err.values()):,} calls failed; "
           f"{dl:,} would have hit a wallet's own deadline.")
    for (s, step, meth, status, purpose, detail), (n, secs) in failures(m).items():
        txt += (f" {E(s)}: {n} × <code>{E(meth)}</code> {E(status)} in <code>{E(step)}</code>"
                + (f" ({E(purpose)})" if purpose else "") + f" after a median {fmt(secs, 's')}"
                + (f", <q>{E(detail)}</q>" if detail else "") + ".")
    inc = [(t, s, n) for t in m.tasks for s, n in (t.get("failed") or {}).items() if n]
    if inc:
        txt += " Incomplete repetitions, whose times are not comparable: " + "; ".join(
            f"{E(s)} {E(t['label'])} ({n} of {len(t['reps'].get(s, []))})" for t, s, n in inc) + "."
    out.append(txt)
    return out


def failures(m: Model) -> dict:
    """(service, step, method, status, purpose, detail with hashes elided) -> (calls, median seconds)."""
    import re
    import statistics
    g = defaultdict(list)
    for c in m.calls:
        if c.get("ok") or not c.get("step") or c.get("scenario") == "stress":
            continue
        d = re.sub(r"\b[0-9a-f]{16,}\b", "…", c.get("detail") or "")[:160]
        g[(c["service"], c["step"], c["method"], c.get("status") or "", c.get("purpose") or "", d)].append(
            (c.get("latency_ms") or 0) / 1000)
    return {k: (len(v), statistics.median(v)) for k, v in sorted(g.items(), key=lambda kv: -len(kv[1]))[:6]}


def errors_total(m: Model) -> dict:
    out = {s: dict(calls=0, errors=0, statuses=Counter(), deadline=0, checked=0) for s in m.services}
    for st in m.steps.values():
        for s, ps in st["per_service"].items():
            if s not in out:
                continue
            for meth, d in (ps.get("methods") or {}).items():
                out[s]["calls"] += d.get("calls") or 0
                out[s]["errors"] += d.get("errors") or 0
                for k, v in (d.get("errors_by_status") or {}).items():
                    out[s]["statuses"][k] += v
            for wallet, dd in (ps.get("deadlines") or {}).items():
                if wallet != PROFILE_KEY(st):
                    continue
                for _name, (hit, checked) in dd.items():
                    out[s]["deadline"] += hit
                    out[s]["checked"] += checked
    return out


def PROFILE_KEY(st):  # noqa: N802 - the profile whose deadlines apply to a step
    return (st.get("params") or {}).get("profile") or "vizor"


def step_panel(m: Model, sid: str) -> str:
    st = m.steps[sid]
    ps = st["per_service"]
    svcs = [s for s in m.services if s in ps]
    h = []
    p = st.get("params") or {}
    reps = {s: ps[s].get("reps") for s in svcs}
    failed = {s: ps[s].get("reps_failed") for s in svcs if ps[s].get("reps_failed")}
    h.append(f'<p class="meta">Scenario <code>{E(st["scenario"])}</code> · '
             + " · ".join(f"{E(k)} {E(str(v))}" for k, v in p.items())
             + f" · {max(reps.values() or [0])} repetition(s) per service"
             + (f" · failed: {E(json.dumps(failed))}" if failed else "") + "</p>")
    head = ["Metric"] + [svc_label(m, s) for s in svcs]
    comp = st.get("compare") or {}

    def cell(d, unit, metric_key, s, lower):
        if not d or not d.get("n"):
            return "–"
        v = d.get("p50")
        ci = d.get("median_ci95")
        extra = f'<em>{E(fmt(ci[0], unit))} – {E(fmt(ci[1], unit))}</em>' if ci and d["n"] >= 3 else \
            f'<em>n={d["n"]}</em>'
        c = comp.get(metric_key) or {}
        cls = ""
        if c.get("best") == s:
            cls = ' class="best"'
        elif ((c.get("vs_best") or {}).get(s) or {}).get("significant") is False:
            cls = ' class="tie"'
        return f"<span{cls}>{E(fmt(v, unit))}</span>{extra}"

    # Per-repetition metrics.
    keys = []
    for s in svcs:
        for k, d in (ps[s].get("rep_stats") or {}).items():
            if k not in keys and d.get("n"):
                keys.append(k)
    rows = []
    for k in keys:
        vals = [((ps[s].get("rep_stats") or {}).get(k) or {}) for s in svcs]
        if all(d.get("stdev", 0) == 0 and d.get("p50") == vals[0].get("p50") for d in vals) and k not in comp:
            continue  # the same constant on every service (blocks_expected, seed, ...)
        u = unit_of(k)
        rows.append([f'{E(METRIC_LABEL.get(k, k))} <em>{E(u)}</em>']
                    + [cell(d, u, k, s, k not in HIGHER_BETTER) for d, s in zip(vals, svcs)])
    h.append('<h3>Per repetition <span>median, 95% CI below</span></h3>')
    h.append(_table(head, rows))

    # Calls.
    meths = sorted({mm for s in svcs for mm in (ps[s].get("methods") or {})})
    rows = []
    for mm in meths:
        cells = []
        for s in svcs:
            d = (ps[s].get("methods") or {}).get(mm) or {}
            lat = d.get("latency_ms") or {}
            if not lat.get("n"):
                cells.append(f"<em>{d.get('calls', 0)} calls</em>" if d else "–")
                continue
            ckey = f"{mm} latency_ms"
            c = comp.get(ckey) or {}
            cls = ' class="best"' if c.get("best") == s else (
                ' class="tie"' if ((c.get("vs_best") or {}).get(s) or {}).get("significant") is False else "")
            err = f' · <b class="err">{d["errors"]} err</b>' if d.get("errors") else ""
            cells.append(f'<span{cls}>{E(fmt(lat["p50"], "ms"))}</span><em>p95 {E(fmt(lat.get("p95"), "ms"))} · '
                         f'n={lat["n"]}{err}</em>')
        rows.append([f"<code>{E(mm)}</code>"] + cells)
    if rows:
        h.append('<h3>Call latency <span>warm calls · median, p95 and count below</span></h3>')
        h.append(_table(["RPC"] + head[1:], rows))

    # Block streams.
    bs = {s: ps[s].get("block_streams") for s in svcs if ps[s].get("block_streams")}
    if bs:
        def g(s, k, q="p50"):
            return ((bs.get(s) or {}).get(k) or {}).get(q)
        rows = [["First block <em>p50 · p95</em>"] + [f'{E(fmt(g(s, "first_message_ms"), "ms"))}<em>p95 '
                                                      f'{E(fmt(g(s, "first_message_ms", "p95"), "ms"))}</em>' for s in svcs],
                ["Transfer rate after the first block <em>p50</em>"] + [E(fmt(g(s, "transfer_mbytes_per_s"), "MB/s"))
                                                                        for s in svcs],
                ["Longest gap between blocks <em>p99</em>"] + [E(fmt(g(s, "max_gap_ms", "p99"), "ms")) for s in svcs],
                ["Stalls <em>gaps above %g s</em>" % (m.opts.get("stall_s") or 2)] +
                [f'{(bs.get(s) or {}).get("stalls", 0)}<em>{E(fmt((bs.get(s) or {}).get("stall_s", 0), "s"))}</em>'
                 for s in svcs]]
        h.append(f'<h3>GetBlockRange streams <span>{max((b or {}).get("streams", 0) for b in bs.values())} '
                 f'streams per service</span></h3>')
        h.append(_table(["Metric"] + head[1:], rows))

    # Errors and deadlines, only when there are any.
    bad = []
    for s in svcs:
        for mm, d in (ps[s].get("methods") or {}).items():
            if d.get("errors"):
                bad.append([svc_label(m, s), f"<code>{E(mm)}</code>", f'{d["errors"]} of {d["calls"]}',
                            E(", ".join(f"{k} {v}" for k, v in (d.get("errors_by_status") or {}).items()))])
        for wallet, dd in (ps[s].get("deadlines") or {}).items():
            for name, (hit, checked) in dd.items():
                if hit:
                    bad.append([svc_label(m, s), f"{E(wallet)} deadline", f"{hit} of {checked}", E(name)])
    if bad:
        h.append("<h3>Errors and deadline hits</h3>")
        h.append(_table(["Service", "What", "Calls", "Detail"], bad))
    cc = st.get("crosscheck") or {}
    if cc:
        parts = [f"{v.get('compared', 0):,} {E(correctness_names().get(k, k))}"
                 + (f", <b class='err'>{v['mismatches']} mismatched</b>" if v.get("mismatches") else "")
                 for k, v in (cc.get("kinds") or {}).items()]
        h.append(f'<p class="meta">Compared across services: {"; ".join(parts)}.</p>')
    return "".join(h)


def correctness_names():
    return {"block": "blocks", "tree_state": "tree states", "subtree_roots": "subtree-root lists",
            "tx": "transactions", "taddr_txids": "address tx lists", "utxos": "UTXO sets"}


def _table(head, rows, cls="stats"):
    return (f'<div class="table-wrap"><table class="{cls}"><thead><tr>' + "".join(f"<th>{x}</th>" for x in head)
            + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
            + "</tbody></table></div>")


def when(run) -> str:
    try:
        t = _dt.datetime.strptime(run.get("started_utc", ""), "%Y%m%dT%H%M%SZ")
        return t.strftime("%B %-d, %Y · %H:%M UTC")
    except ValueError:
        return run.get("started_utc", "")


def duration(run) -> str:
    try:
        a = _dt.datetime.strptime(run["started_utc"], "%Y%m%dT%H%M%SZ")
        b = _dt.datetime.strptime(run["ended_utc"], "%Y%m%dT%H%M%SZ")
        return f"{(b - a).total_seconds() / 60:.0f} min"
    except (KeyError, ValueError):
        return "–"


def build_site(run_dir, title: str | None = None) -> pathlib.Path:
    sp.setup()
    m = Model(run_dir)
    out = m.run_dir / "site"
    if out.exists():
        for sub in ("plots", "data"):
            shutil.rmtree(out / sub, ignore_errors=True)
    out.mkdir(exist_ok=True)
    shutil.copy(ASSETS / "site.css", out / "site.css")
    shutil.copy(ASSETS / "site.js", out / "site.js")
    copy_brand(out)
    meta = m.run.get("meta") or {}
    loc = meta.get("location") or {}
    own = [s for s in m.services if (m.svc.get(s) or {}).get("own")]
    third = [s for s in m.services if s not in own]
    if not title:
        title = (" and ".join(own) + " vs " + " and ".join(third)) if own and third else " vs ".join(m.services)

    page = Page()
    # Summary
    page.section("summary", "Summary")
    profs = sorted({(m.steps[t["step"]].get("params") or {}).get("profile") for t in m.tasks} - {None},
                   key=lambda p: list(PROFILE).index(p) if p in PROFILE else 9)
    page.p(f"Each wallet task below replays the requests of a real wallet ({', '.join(PROFILE.get(p, p) for p in profs)}) "
           f"against every service, one service at a time, with the order rotated each repetition. Times are what the "
           f"wallet waits, measured at a client in {E(loc.get('cloudflare_site', '?'))} "
           f"({E(loc.get('country', '?'))}), so they include the network: about "
           f"{m.rtt_ms:.1f} ms of round trip to each service." if m.rtt_ms else "")
    page.bullets(findings(m))
    page.add('<h3>Scoreboard <span>median per task · bar: relative to the slowest · ≈ best: not distinguishable '
             'from the best in this run</span></h3>')
    page.add(scoreboard(m, m.tasks + scale_rows(m)))

    # Setup
    page.section("setup", "Setup")
    rows = []
    for s in m.services:
        sv = m.svc.get(s) or {}
        li = sv.get("lightd_info") or {}
        cn = sv.get("connection") or {}
        rows.append([svc_label(m, s), E(sv.get("kind", "")), f"<code>{E(sv.get('endpoint', ''))}</code>"
                     + (f"<em>{E(sv['note'])}</em>" if sv.get("note") else ""),
                     E(f"{li.get('vendor', '')} {li.get('version', '')}".strip() or "–"),
                     E((li.get("node_subversion") or "–").strip("/")),
                     E(fmt(cn.get("tcp_connect_ms_min"), "ms")), E(fmt(cn.get("tls_handshake_ms_p50"), "ms"))])
    page.table(["Service", "Kind", "Endpoint", "Server", "Node", "RTT (TCP min)", "TLS p50"], rows, "setup")
    fx = m.run.get("fixtures_file") or {}
    commit = (meta.get("harness_commit") or "")[:10]
    page.table(["", ""], [
        ["Client", f"Cloudflare site {E(loc.get('cloudflare_site', '?'))}, {E(loc.get('country', '?'))} · "
                   f"{E(str(meta.get('logical_cpus', '?')))} logical CPUs · {E(meta.get('os', ''))} "
                   f"{E(meta.get('machine', ''))}"],
        ["Software", f"Python {E(meta.get('python', ''))} · grpcio {E(meta.get('grpcio', ''))} · protobuf "
                     f"{E(meta.get('protobuf', ''))} · HTTP/2 window {E(str(m.run.get('h2_window', '')))}"],
        ["Run", f"{E(when(m.run))} · {duration(m.run)} · label <code>{E(m.run.get('label', ''))}</code>"],
        ["Harness", f"<code>{E(commit)}</code>" + (" (uncommitted changes)" if meta.get("harness_dirty") else "")],
        ["Fixtures", E(str(fx.get("name") or fx.get("path") or "mainnet.json"))
         + (f" · sha256 <code>{E(str(fx.get('sha256', ''))[:12])}</code>" if isinstance(fx, dict) and fx.get("sha256") else "")],
    ], "kv")
    page.note("A call cannot finish faster than one network round trip. Everything above the RTT is the server, its "
              "proxy, or its node. Python parses responses after each call ends, so client parsing is not in the times.")

    # Wallet tasks
    page.section("tasks", "Wallet tasks")
    page.p("Every repetition of every task, on one log axis. Where dots of two services overlap, this run cannot "
           "separate them; the bar is the 95% bootstrap CI of the median.")
    page.fig(sp.tasks_figure(m, out), "Dot plot of wall time per wallet task and service")

    # Phases and download
    if any(t.get("phases") for t in m.tasks):
        page.section("phases", "Where the time goes", "Time split")
        page.p("A sync has three phases: the calls before the first block request (latest block, UTXO refresh, "
               "subtree roots, tree state), the block download, and the calls after the last block. A slow first "
               "phase delays the whole sync, however fast the blocks stream.")
        page.fig(sp.phases_figure(m, out), "Stacked bars of sync phases per step and service")
        page.section("download", "Block download")
        page.p("Blocks received over time in each repetition, from the first call of the repetition. The slope is "
               "the block rate, a late start is the first phase, a flat stretch is a stall.")
        page.fig(sp.download_figure(m, out), "Cumulative blocks received over time per repetition")

    # RPC latency
    if m.latency_panels:
        page.section("latency", "RPC latency")
        page.p("The distribution of every warm call (not the first call on a connection), pooled over all steps. "
               "A curve to the left is faster; a long flat top is a slow tail.")
        page.fig(sp.latency_figure(m, out), "Latency CDF per RPC and service")

    # Load
    if m.scale:
        page.section("load", "Wallets at once", "Load")
        page.p(f"1 to {max(int(k) for per in m.scale.values() for k in per)} wallets start the same catch-up at the "
               f"same moment, each on its own connection. A service that scales keeps the per-wallet rate while the "
               f"total grows.")
        page.fig(sp.load_figure(m, out), "Load curve: MB/s and first-block time against wallets at once")

    # Tips
    if m.tip_lag:
        page.section("tips", "Tip freshness", "Tips")
        page.p("A wallet that asks a service behind the tip sees new transactions later. Each snapshot asks every "
               "service for its latest block at the same moment.")
        page.fig(sp.tips_figure(m, out), "Share of snapshots behind the highest tip, per service")
        tf = m.steps.get("tip-follow")
        if tf:
            rows = []
            for s in m.services:
                rs = ((tf["per_service"].get(s) or {}).get("rep_stats") or {})
                g = lambda k, q="p50": (rs.get(k) or {}).get(q)  # noqa: E731
                rows.append([svc_label(m, s), E(fmt(g("polls"), "count")), E(fmt(g("poll_errors"), "count")),
                             E(fmt(g("mempool_txs_seen"), "count")), E(fmt(g("mempool_stream_opens"), "count")),
                             E(fmt(g("block_arrival_delay_s"), "s")) + f'<em>p95 {E(fmt(g("block_arrival_delay_s", "p95"), "s"))}</em>'])
            page.add(f'<h3>Idle wallet, app open <span>{E(sp.dur(float((tf.get("params") or {}).get("duration_s") or 0)))} · '
                     f'poll every 10 s, mempool stream, catch-up on a new block</span></h3>')
            page.table(["Service", "Polls", "Poll errors", "Mempool txs seen", "Mempool stream opens",
                        "New block delay p50"], rows, "stats")

    # Correctness and errors
    cc = correctness(m)
    page.section("correctness", "Correctness and errors", "Correctness")
    if cc["steps"]:
        rows = []
        for k, (n, mis, aux) in sorted(cc["kinds"].items(), key=lambda kv: -kv[1][0]):
            rows.append([E(cc["names"].get(k, k)), f"{n:,}",
                         (f'<b class="err">{mis:,}</b>' if mis else '<span class="good">0</span>'),
                         f"{aux:,}" if aux else "0"])
        page.table(["Compared", "Items", "Mismatched", "Aux-only differences"], rows, "stats")
        if cc["odd"]:
            page.p("Outside the majority: " + ", ".join(f"{E(s)} {n}" for s, n in cc["odd"].items()) + ".")
        if cc["unstable"]:
            groups = defaultdict(set)
            for sid, u in cc["unstable"]:
                groups[(sid, u.get("kind"), u.get("key"), u.get("rep"))].add(u.get("service"))
            everywhere = all(v >= set(m.services) for v in groups.values())
            steps = sorted({g[0] for g in groups})
            if everywhere:
                page.note(f"In {', '.join(f'<code>{E(s)}</code>' for s in steps)} an answer changed between "
                          "repetitions on <em>every</em> service at the same repetition: the live chain moved (for "
                          "example a UTXO was spent). The services still agreed with each other, so the step's "
                          "<code>MISMATCH</code> label in report.md is not a disagreement between services.")
            else:
                page.note("Answers that changed between repetitions of the same service: " + "; ".join(
                    f"<code>{E(k[0])}</code> {E(str(k[1]))} rep {k[3]}: {', '.join(sorted(v))}"
                    for k, v in sorted(groups.items(), key=str)) + ".")
    err = errors_total(m)
    rows = []
    for s in m.services:
        e = err[s]
        rows.append([svc_label(m, s), f"{e['calls']:,}",
                     (f'<b class="err">{e["errors"]:,}</b>' if e["errors"] else '<span class="good">0</span>'),
                     E(", ".join(f"{k} {v}" for k, v in e["statuses"].most_common())) or "–",
                     (f'<b class="err">{e["deadline"]:,}</b>' if e["deadline"] else '<span class="good">0</span>')
                     + f'<em>of {e["checked"]:,} checked</em>'])
    page.table(["Service", "Calls", "Failed", "By status", "Over the wallet's deadline"], rows, "stats")

    # Step details
    page.section("details", "Step details", "Details")
    page.p("All statistics of one step. Bold: the best median; dim: not distinguishable from the best. "
           "<a href=\"../report.md\">report.md</a> has every percentile.")
    opts = "".join(f'<option value="s{i}">{E(sid)}</option>' for i, sid in enumerate(m.steps))
    page.add(f'<label class="picker">Step <select id="step-pick" aria-label="Step">{opts}</select></label>')
    for i, sid in enumerate(m.steps):
        page.add(f'<div class="step" id="s{i}"><h3 class="step-title">{E(sid)}</h3>{step_panel(m, sid)}</div>')

    # Raw data
    page.section("data", "Raw data")
    raw = [("../raw/calls.jsonl.gz", "Every call"), ("../raw/messages.csv.gz", "Every stream message"),
           ("../raw/connections.jsonl.gz", "Connections"), ("../raw/runs.jsonl.gz", "Repetitions"),
           ("../raw/tips.jsonl.gz", "Tip snapshots"), ("../raw/crosscheck.json", "Cross-check digests"),
           ("../summary.json", "All statistics"), ("../run.json", "Options, services, profiles, plan"),
           ("../report.md", "Markdown report with every table")]
    page.p(" · ".join(f'<a href="{h}">{E(t)}</a>' for h, t in raw if (m.run_dir / h[3:]).exists()) +
           ". Figure data is linked under each figure. Rebuild this page with "
           f"<code>zcash-walletsync-bench site {E(str(m.run_dir))}</code>; it reads only these files.")

    eyebrow = f"// Measured {when(m.run)} · client in {loc.get('cloudflare_site', '?')}, {loc.get('country', '?')}"
    sub = ("Wallet-shaped traffic against " + ", ".join(
        f'<strong>{E(s)}</strong> ({E(((m.svc.get(s) or {}).get("lightd_info") or {}).get("vendor", "?"))} '
        f'{E(((m.svc.get(s) or {}).get("lightd_info") or {}).get("version", ""))} on '
        f'{E((((m.svc.get(s) or {}).get("lightd_info") or {}).get("node_subversion") or "?").strip("/"))})'
        for s in m.services) + ". Every service got the same requests through the public lightwalletd gRPC API.")
    html_ = render(page, title, eyebrow, sub, cards(m), m)
    (out / "index.html").write_text(html_)
    return out / "index.html"


def render(page: Page, title, eyebrow, sub, cards_, m: Model) -> str:
    nav = "".join(f'<a href="#{s["id"]}">{E(s["nav"])}</a>' for s in page.sections)
    h = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width, initial-scale=1">',
         f"<title>{E(title)} · zcash-walletsync-bench</title>", '<meta name="theme-color" content="#171415">',
         '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Sora:wght@700;800&family=Manrope:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">',
         '<link rel="icon" href="favicon.svg" type="image/svg+xml"><link rel="stylesheet" href="site.css"></head><body class="benchmarks-page">',
         '<header><div class="wrap"><a class="brand" href="#top"><img src="mark.svg" alt="" width="40" height="40"><b>zcash-walletsync-bench</b>'
         f'<span>{E(m.run.get("label", ""))}</span></a><nav><a href="#details">Details</a>'
         '<a href="../report.md">Report (md)</a><a href="#data">Raw data</a></nav></div></header>',
         '<main class="wrap benchmark-report" id="top">',
         f'<div class="eyebrow">{E(eyebrow)}</div><h1>{E(title)}</h1><p class="sub">{sub}</p>',
         f'<nav class="report-nav" aria-label="Sections">{nav}</nav>',
         f'<div class="cards cards-6">{"".join(cards_)}</div>']
    for s in page.sections:
        h.append(f'<section id="{s["id"]}"><h2>{E(s["title"])}</h2>{"".join(s["body"])}</section>')
    h.append('</main><footer><div class="wrap"><a class="by" href="https://zcashlabs.com"><img src="mark.svg" alt="" width="40" height="40">A Zcash Labs project</a><span>Generated by zcash-walletsync-bench from run.json and raw/</span>'
             '<nav><a href="#top">Top</a></nav></div></footer><script src="site.js"></script></body></html>')
    return "".join(h)
