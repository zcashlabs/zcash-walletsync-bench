"""Region planning from a geo sweep: where to add fastz regions to match or beat zec.rocks.

    zcash-walletsync-bench geo-map results/<sweep-dir>     # writes <sweep>/site/map.html

Per city it takes the measured latency to every fastz region and every zec.rocks zone, minus the tunnel
baseline (the VPN hop, the same for every host in that city). A fastz region in a new place has not been
measured, so its latency is predicted from distance with a line fitted to the measured fastz regions
(robust, least absolute deviation). Regions are then picked greedily: each step adds the candidate that
makes fastz match or beat zec.rocks in the most cities.

Assumptions, all shown on the page: every city counts the same; DNS steers each city to its fastest fastz
region (the sweep found 7 cities where it does not today); zec.rocks keeps its current sites; the distance
model has a median error of about 5 ms within 3000 km and much more across oceans.
"""

from __future__ import annotations

import json
import math
import pathlib
import statistics

from .geosweep import best_records, read_jsonl
from .brand import copy_brand

# Existing fastz regions (hosts behind {usw,use,eu,ap}.fastz.cash).
REGIONS = {
    "usw": ("Hillsboro", 45.54, -122.94),
    "use": ("Warrenton", 38.72, -77.80),
    "eu": ("Frankfurt", 50.11, 8.68),
    "ap": ("Singapore", 1.35, 103.82),
    "sa": ("São Paulo", -23.55, -46.63),
    "jp": ("Tokyo", 35.68, 139.69),
    "lon": ("London", 51.51, -0.13),
}

# Candidate sites: major interconnection metros. "ovh" marks metros where OVHcloud (the current host) has a
# region; any provider with good local peering works for the model.
CANDIDATES = [
    ("Los Angeles", 34.05, -118.24, ""), ("Dallas", 32.78, -96.80, ""), ("Chicago", 41.88, -87.63, ""),
    ("Miami", 25.76, -80.19, ""), ("Beauharnois (Montreal)", 45.31, -73.87, "ovh"),
    ("Querétaro (Mexico City)", 20.59, -100.39, ""), ("Bogotá", 4.71, -74.07, ""),
    ("São Paulo", -23.55, -46.63, ""), ("Santiago", -33.45, -70.67, ""), ("Buenos Aires", -34.60, -58.38, ""),
    ("London", 51.51, -0.13, "ovh"), ("Paris (Gravelines/Roubaix)", 50.69, 3.17, "ovh"),
    ("Amsterdam", 52.37, 4.90, ""), ("Madrid", 40.42, -3.70, ""), ("Milan", 45.46, 9.19, ""),
    ("Stockholm", 59.33, 18.07, ""), ("Warsaw", 52.23, 21.01, "ovh"), ("Istanbul", 41.01, 28.98, ""),
    ("Tel Aviv", 32.09, 34.78, ""), ("Dubai", 25.20, 55.27, ""), ("Johannesburg", -26.20, 28.05, ""),
    ("Lagos", 6.52, 3.38, ""), ("Mumbai", 19.08, 72.88, "ovh"), ("Hong Kong", 22.32, 114.17, ""),
    ("Tokyo", 35.68, 139.69, ""), ("Seoul", 37.57, 126.98, ""), ("Taipei", 25.03, 121.57, ""),
    ("Sydney", -33.87, 151.21, "ovh"), ("Melbourne", -37.81, 144.96, ""), ("Auckland", -36.85, 174.76, ""),
    ("Jakarta", -6.21, 106.85, ""),
]


def km(a, b) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, h)))


def _p50(rounds, name, min_n):
    v = [r["ms"][name] for r in rounds if name in r["ms"]]
    return statistics.median(v) if len(v) >= min_n else None


def city_table(sweep) -> list[dict]:
    """One row per city: tunnel baseline, and latency minus the tunnel to each fastz region, the best
    zec.rocks zone and zcashlabs (medians over all rounds)."""
    recs = best_records(read_jsonl(pathlib.Path(sweep) / "cities.jsonl"))
    out = []
    for key, r in sorted(recs.items()):
        rounds = r.get("rounds") or []
        if not rounds:
            continue
        min_n = max(3, len(rounds) // 2)
        base = [x["baseline_ms"] for x in rounds if x.get("baseline_ms") is not None]
        tunnel = statistics.median(base) if base else 0.0
        fz = {g: _p50(rounds, "fastz-" + g, min_n) for g in REGIONS}
        fz = {g: round(v - tunnel, 1) for g, v in fz.items() if v is not None}
        zr = {}
        for x in ("global", "na", "sa", "eu", "ap", "me"):
            v = _p50(rounds, "zecrocks-" + x, min_n)
            if v is not None:
                zr[x] = round(v - tunnel, 1)
        dns = _p50(rounds, "fastz", min_n)
        zl = _p50(rounds, "zcashlabs", min_n)
        if not fz or not zr:
            continue
        zbest = min(zr, key=zr.get)
        # Share of calls more than 100 ms above their own target's median: a lossy exit (Reykjavik, Oslo).
        meds = {t: statistics.median([x["ms"][t] for x in rounds if t in x["ms"]])
                for t in {t for x in rounds for t in x["ms"]}}
        spikes = sum(1 for x in rounds for t, v in x["ms"].items() if v > meds[t] + 100) / max(
            1, sum(len(x["ms"]) for x in rounds))
        out.append({
            "key": key, "name": r.get("name"), "country": r.get("country"), "lat": r["lat"], "lon": r["lon"],
            "tunnel": round(tunnel, 1), "fastz": fz, "fastz_dns": round(dns - tunnel, 1) if dns else None,
            "dns_region": r.get("fastz_dns_region"), "zecrocks": zr, "zecrocks_best": zbest,
            "zecrocks_ms": zr[zbest], "zcashlabs": round(zl - tunnel, 1) if zl else None,
            "noisy": spikes > 0.08,
        })
    return out


def fit_model(cities: list[dict], max_km: float = 3000.0) -> dict:
    """latency_ms = a + b * km, least absolute deviation over the (city, fastz region) pairs closer than
    max_km. A new region only wins cities near it, so the near range is the one that matters; across oceans
    routes are far from straight and the error is large."""
    import numpy as np
    X, Y = [], []
    for c in cities:
        for g, v in c["fastz"].items():
            X.append(km((c["lat"], c["lon"]), REGIONS[g][1:]))
            Y.append(v)
    X, Y = np.asarray(X), np.asarray(Y)
    keep = X < max_km
    X, Y = X[keep], Y[keep]
    A = np.c_[np.ones_like(X), X]
    w = np.ones_like(X)
    for _ in range(60):
        sw = np.sqrt(w)
        coef = np.linalg.lstsq(A * sw[:, None], Y * sw, rcond=None)[0]
        w = 1 / np.maximum(np.abs(Y - A @ coef), 1.0)
    res = np.abs(Y - A @ coef)
    near = X < 3000
    return {"a": round(float(coef[0]), 2), "fit_km": max_km, "b_per_1000km": round(float(coef[1]) * 1000, 2), "n": int(X.size),
            "abs_err_p50": round(float(np.median(res)), 1), "abs_err_p90": round(float(np.percentile(res, 90)), 1),
            "near_abs_err_p50": round(float(np.median(res[near])), 1) if near.any() else None}


def predict(model: dict, a, b) -> float:
    return model["a"] + model["b_per_1000km"] * km(a, b) / 1000


def evaluate(cities, model, added: list[int], tol: float) -> dict:
    """fastz latency per city with the added candidates (best of measured regions and predictions), and
    how many cities fastz matches or beats zec.rocks within `tol` ms."""
    won = 0
    per = []
    for c in cities:
        best, via = min((v, g) for g, v in c["fastz"].items())
        for i in added:
            name, lat, lon, _ = CANDIDATES[i]
            p = predict(model, (c["lat"], c["lon"]), (lat, lon))
            if p < best:
                best, via = p, name
        ok = best <= c["zecrocks_ms"] + tol
        won += ok
        per.append((round(best, 1), via, ok))
    return {"won": won, "per": per}


def greedy(cities, model, tol: float, steps: int = 8) -> list[dict]:
    chosen, out = [], []
    for _ in range(steps):
        base = evaluate(cities, model, chosen, tol)
        best = None
        for i in range(len(CANDIDATES)):
            if i in chosen:
                continue
            e = evaluate(cities, model, chosen + [i], tol)
            margin = sum(c["zecrocks_ms"] - p[0] for c, p in zip(cities, e["per"]))
            key = (e["won"], margin)
            if best is None or key > best[0]:
                best = (key, i, e)
        if best is None or best[2]["won"] <= base["won"]:
            break
        chosen.append(best[1])
        newly = [c["name"] for c, p0, p1 in zip(cities, base["per"], best[2]["per"]) if p1[2] and not p0[2]]
        out.append({"site": CANDIDATES[best[1]][0], "index": best[1], "won": best[2]["won"], "newly": newly})
    return out


def zecrocks_sites(cities, near_ms: float = 12.0) -> list[dict]:
    """Cities that reach a zec.rocks zone in under near_ms after the tunnel: a zec.rocks server is close."""
    return [{"name": c["name"], "lat": c["lat"], "lon": c["lon"], "zone": c["zecrocks_best"], "ms": c["zecrocks_ms"]}
            for c in cities if c["zecrocks_ms"] < near_ms]


def plan(sweep, tol: float = 2.0) -> dict:
    cities = city_table(sweep)
    model = fit_model(cities)
    now_best = evaluate(cities, model, [], tol)["won"]
    now_dns = sum(1 for c in cities if c["fastz_dns"] is not None and c["fastz_dns"] <= c["zecrocks_ms"] + tol)
    return {"cities": cities, "model": model, "regions": REGIONS, "candidates": CANDIDATES, "tol": tol,
            "now_dns": now_dns, "now_best": now_best, "greedy": greedy(cities, model, tol),
            "zecrocks_sites": zecrocks_sites(cities)}


def summary_text(p: dict) -> str:
    n = len(p["cities"])
    m = p["model"]
    lines = ["Cities: %d. Model: %.1f ms + %.1f ms per 1000 km (median error %.1f ms; %.1f ms within 3000 km)."
             % (n, m["a"], m["b_per_1000km"], m["abs_err_p50"], m["near_abs_err_p50"]),
             "fastz matches or beats zec.rocks (within %g ms) today in %d cities as DNS steers, %d with the best "
             "region." % (p["tol"], p["now_dns"], p["now_best"])]
    for k, s in enumerate(p["greedy"], 1):
        lines.append("  +%d %-28s %3d/%d (%.0f%%)  new: %s" % (k, s["site"], s["won"], n, 100 * s["won"] / n,
                                                             ", ".join(s["newly"])))
    return "\n".join(lines)


# ------------------------------------------------------------------ the map page

MAP_JS = r"""
const D = JSON.parse(document.getElementById('plan-data').textContent);
const COL = {fastz: '#3987e5', zec: '#199e70', tie: '#d6d0d1', ink: '#f4f4f4', dim: '#a3a3a3', faint: '#6f6a6b',
             rule: '#363132', panel: '#211d1e', bg: '#171415'};
const state = {added: new Set(), tol: D.tol, floor: D.model.a};
const R = Math.PI / 180;
function km(a, b) {
  const p1 = a[0] * R, p2 = b[0] * R, dl = (b[1] - a[1]) * R;
  const h = Math.sin((p2 - p1) / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.sqrt(Math.min(1, h)));
}
function pred(c, k) { const s = D.candidates[k]; return state.floor + D.model.b_per_1000km * km([c.lat, c.lon], [s[1], s[2]]) / 1000; }
function evaluate(added) {
  return D.cities.map(c => {
    let best = Infinity, via = '';
    for (const [g, v] of Object.entries(c.fastz)) if (v < best) { best = v; via = 'fastz ' + g + ' (measured)'; }
    for (const k of added) { const p = pred(c, k); if (p < best) { best = p; via = D.candidates[k][0] + ' (predicted)'; } }
    const m = c.zecrocks_ms - best;
    const cls = m > state.tol ? 'fastz' : (m < -state.tol ? 'zec' : 'tie');
    return {best, via, margin: m, cls, ok: cls !== 'zec'};
  });
}
const won = e => e.filter(x => x.ok).length;
function greedy(steps) {
  const chosen = [], out = [];
  for (let s = 0; s < steps; s++) {
    const base = evaluate(chosen); let best = null;
    D.candidates.forEach((_, k) => {
      if (chosen.includes(k)) return;
      const e = evaluate([...chosen, k]);
      const key = [won(e), e.reduce((a, x) => a + x.margin, 0)];
      if (!best || key[0] > best.key[0] || (key[0] === best.key[0] && key[1] > best.key[1])) best = {key, k, e};
    });
    if (!best || best.key[0] <= won(base)) break;
    chosen.push(best.k);
    out.push({k: best.k, won: best.key[0],
              newly: D.cities.filter((c, i) => best.e[i].ok && !base[i].ok).map(c => c.name)});
  }
  return out;
}

const W = 1100, H = 560;
const svg = d3.select('#map').attr('viewBox', `0 0 ${W} ${H}`);
const proj = d3.geoEqualEarth();
const path = d3.geoPath(proj);
const tip = d3.select('#tip');
const fmt = v => v == null ? '–' : (Math.round(v * 10) / 10) + ' ms';
const gBase = svg.append('g');  // drawn first: the outline loads later and must stay underneath

d3.json('https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/land-110m.json').then(topo => {
  const land = topojson.feature(topo, topo.objects.land);
  proj.fitExtent([[8, 8], [W - 8, H - 8]], {type: 'Sphere'});
  gBase.append('path').attr('d', path({type: 'Sphere'})).attr('fill', COL.bg).attr('stroke', COL.rule);
  gBase.append('path').attr('d', path(d3.geoGraticule10())).attr('fill', 'none').attr('stroke', COL.ink).attr('stroke-opacity', .05);
  gBase.append('path').attr('d', path(land)).attr('fill', COL.panel).attr('stroke', '#4a4445').attr('stroke-width', .6);
  draw();
}).catch(() => { svg.append('text').attr('x', 20).attr('y', 30).attr('fill', COL.dim).text('World outline could not load (offline); points are still placed by longitude/latitude.'); proj.fitExtent([[8, 8], [W - 8, H - 8]], {type: 'Sphere'}); draw(); });

const gZec = svg.append('g'), gCities = svg.append('g'), gReg = svg.append('g'), gCand = svg.append('g');

function showTip(ev, html) {
  const box = document.getElementById('mapwrap').getBoundingClientRect();
  tip.html(html).style('display', 'block');
  const tw = tip.node().offsetWidth;
  let x = ev.clientX - box.left + 14; if (x + tw > box.width) x = ev.clientX - box.left - tw - 14;
  tip.style('left', Math.max(0, x) + 'px').style('top', (ev.clientY - box.top + 14) + 'px');
}
const hideTip = () => tip.style('display', 'none');

function draw() {
  const e = evaluate([...state.added]);
  // zec.rocks servers inferred from near-zero latency
  gZec.selectAll('path').data(D.zecrocks_sites).join('path')
    .attr('d', d3.symbol(d3.symbolTriangle, 70)()).attr('transform', d => `translate(${proj([d.lon, d.lat])})`)
    .attr('fill', 'none').attr('stroke', COL.zec).attr('stroke-width', 1.4)
    .on('pointermove', (ev, d) => showTip(ev, `<b>zec.rocks server near ${d.name}</b><br>${d.zone} zone answers in ${fmt(d.ms)} after the VPN hop`))
    .on('pointerleave', hideTip);
  const order = D.cities.map((c, i) => i).sort((a, b) => Math.abs(e[b].margin) - Math.abs(e[a].margin));
  gCities.selectAll('circle').data(order, i => i).join('circle')
    .attr('cx', i => proj([D.cities[i].lon, D.cities[i].lat])[0]).attr('cy', i => proj([D.cities[i].lon, D.cities[i].lat])[1])
    .attr('r', i => 3.5 + Math.min(7, Math.sqrt(Math.abs(e[i].margin)) * .8))
    .attr('fill', i => e[i].cls === 'tie' ? 'none' : COL[e[i].cls])
    .attr('stroke', i => e[i].cls === 'tie' ? COL.tie : COL.bg).attr('stroke-width', i => e[i].cls === 'tie' ? 1.8 : 1.2)
    .on('pointermove', (ev, i) => {
      const c = D.cities[i], x = e[i];
      const regs = Object.entries(c.fastz).sort((a, b) => a[1] - b[1]).map(([g, v]) => `${g} ${fmt(v)}`).join(' · ');
      const verdict = x.cls === 'fastz' ? 'fastz faster' : x.cls === 'zec' ? 'zec.rocks faster' : 'tie (within ±' + state.tol + ' ms)';
      showTip(ev, `<b>${c.name}, ${c.country}</b> <span class="v-${x.cls}">${verdict}</span><br>` +
        `fastz: <b>${fmt(x.best)}</b> via ${x.via}<br>zec.rocks: <b>${fmt(c.zecrocks_ms)}</b> (${c.zecrocks_best})` +
        (c.zcashlabs != null ? `<br>zcashlabs: ${fmt(c.zcashlabs)}` : '') +
        `<br><span class="mute">fastz regions: ${regs}<br>as DNS steers (${c.dns_region || '?'}): ${fmt(c.fastz_dns)}` +
        ` · VPN hop ${fmt(c.tunnel)}${c.noisy ? '<br>lossy exit: values are noisy' : ''}</span>`);
    })
    .on('pointerleave', hideTip);
  // existing regions
  const regs = Object.entries(D.regions).map(([k, v]) => ({k, name: v[0], lat: v[1], lon: v[2]}));
  const rg = gReg.selectAll('g').data(regs).join(enter => { const g = enter.append('g'); g.append('rect'); g.append('text'); return g; });
  rg.attr('transform', d => `translate(${proj([d.lon, d.lat])})`);
  rg.select('rect').attr('x', -6).attr('y', -6).attr('width', 12).attr('height', 12).attr('fill', COL.fastz).attr('stroke', COL.ink).attr('stroke-width', 1.5);
  rg.select('text').attr('x', 10).attr('y', 4).attr('fill', COL.ink).attr('font-size', 12).text(d => 'fastz ' + d.k);
  // candidates
  const cg = gCand.selectAll('g').data(D.candidates.map((c, k) => ({k, name: c[0], lat: c[1], lon: c[2], ovh: c[3]})))
    .join(enter => { const g = enter.append('g').attr('class', 'cand').attr('tabindex', 0).attr('role', 'button');
      g.append('path'); g.append('text'); return g; });
  cg.attr('transform', d => `translate(${proj([d.lon, d.lat])})`).attr('aria-pressed', d => state.added.has(d.k))
    .attr('aria-label', d => 'Candidate region ' + d.name);
  cg.select('path').attr('d', d => d3.symbol(d3.symbolDiamond, state.added.has(d.k) ? 150 : 70)())
    .attr('fill', d => state.added.has(d.k) ? COL.fastz : 'none')
    .attr('stroke', d => state.added.has(d.k) ? COL.ink : COL.dim).attr('stroke-width', 1.4).attr('stroke-dasharray', d => state.added.has(d.k) ? null : '2 2');
  cg.select('text').attr('x', 9).attr('y', -7).attr('font-size', 11).attr('fill', COL.ink)
    .text(d => state.added.has(d.k) ? d.name.split(' (')[0] : '');
  cg.on('click', (ev, d) => toggle(d.k)).on('keydown', (ev, d) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggle(d.k); } })
    .on('pointermove', (ev, d) => showTip(ev, `<b>${d.name}</b>${d.ovh ? ' · OVHcloud region' : ''}<br>${state.added.has(d.k) ? 'Added: click to remove' : 'Candidate: click to add'}`))
    .on('pointerleave', hideTip);
  panel(e);
}

function toggle(k) { state.added.has(k) ? state.added.delete(k) : state.added.add(k); draw(); }

function panel(e) {
  const n = D.cities.length, w = won(e);
  const dns = D.cities.filter(c => c.fastz_dns != null && c.fastz_dns <= c.zecrocks_ms + state.tol).length;
  const best0 = won(evaluate([]));
  document.getElementById('k-dns').textContent = `${dns} / ${n}`;
  document.getElementById('k-best').textContent = `${best0} / ${n}`;
  document.getElementById('k-now').textContent = `${w} / ${n}`;
  document.getElementById('k-now-sub').textContent = `${Math.round(100 * w / n)}% of cities · ${state.added.size} added region${state.added.size === 1 ? '' : 's'}`;
  const g = greedy(14);
  const rows = g.map((s, i) => `<tr><td><button class="apply" data-n="${i + 1}">+${i + 1}</button></td><td>${D.candidates[s.k][0]}` +
    `${D.candidates[s.k][3] ? ' <em>OVHcloud</em>' : ''}</td><td>${s.won} / ${n}<em>${Math.round(100 * s.won / n)}%</em></td>` +
    `<td class="l">${s.newly.join(', ')}</td></tr>`).join('');
  document.getElementById('greedy').innerHTML = rows;
  document.querySelectorAll('button.apply').forEach(b => b.onclick = () => {
    state.added = new Set(g.slice(0, +b.dataset.n).map(s => s.k)); draw(); });
  const lost = D.cities.map((c, i) => [c, e[i]]).filter(([c, x]) => !x.ok).sort((a, b) => a[1].margin - b[1].margin);
  document.getElementById('lost').innerHTML = lost.length ? lost.map(([c, x]) =>
    `<li><b>${c.name}</b> fastz ${fmt(x.best)} vs zec.rocks ${fmt(c.zecrocks_ms)}</li>`).join('') : '<li>None</li>';
  document.getElementById('lost-n').textContent = lost.length;
}

for (const [id, key, f] of [['tol', 'tol', v => v + ' ms'], ['floor', 'floor', v => v.toFixed(1) + ' ms']]) {
  const el = document.getElementById(id), out = document.getElementById(id + '-v');
  el.value = state[key]; out.textContent = f(+el.value);
  el.addEventListener('input', () => { state[key] = +el.value; out.textContent = f(+el.value); draw(); });
}
document.getElementById('clear').onclick = () => { state.added.clear(); draw(); };
"""

MAP_CSS = """
.mapwrap{position:relative;border:1px solid var(--rule);border-radius:var(--radius);background:var(--bg);margin:1rem 0}
#map{display:block;width:100%;height:auto}
#map .cand{cursor:pointer;outline:none}
#map .cand:focus-visible path{stroke:var(--ink);stroke-width:2.5}
#tip{position:absolute;display:none;pointer-events:none;z-index:3;max-width:22rem;background:var(--panel);border:1px solid var(--rule);
 border-radius:var(--radius);padding:.6rem .75rem;font:.72rem/1.55 var(--mono);color:var(--ink)}
#tip .mute{color:var(--dim)} #tip .v-fastz{color:#7fb0f0} #tip .v-zec{color:#4cc79a} #tip .v-tie{color:var(--dim)}
.controls{display:flex;flex-wrap:wrap;gap:1.25rem 2rem;align-items:center;font:.72rem/1.5 var(--mono);color:var(--dim);margin:1rem 0}
.controls label{display:flex;align-items:center;gap:.6rem;text-transform:uppercase;letter-spacing:.1em}
.controls output{color:var(--ink);min-width:3.5rem}
.controls input[type=range]{accent-color:#3987e5;width:9rem}
.benchmark-report button{font:.72rem/1 var(--mono);color:var(--ink);background:var(--panel);border:1px solid var(--rule);
 border-radius:var(--radius);padding:.45rem .6rem;cursor:pointer}
.benchmark-report button:hover{border-color:var(--ink)}
.legend{display:flex;flex-wrap:wrap;gap:.5rem 1.4rem;font:.72rem/1.5 var(--mono);color:var(--dim)}
.legend span{display:inline-flex;align-items:center;gap:.45rem}
.legend i{display:inline-block;width:.7rem;height:.7rem;border-radius:50%}
.benchmark-report table.plan td.l{text-align:left;color:var(--dim);font-size:.68rem;white-space:normal}
.benchmark-report table.plan td:nth-child(2){text-align:left}
ul.lost{columns:2;column-gap:2rem;font:.72rem/1.7 var(--mono);color:var(--dim);margin:.5rem 0 0 1rem}
ul.lost b{color:var(--ink);font-weight:500}
@media(max-width:760px){ul.lost{columns:1}.controls input[type=range]{width:7rem}}
"""


def build_map(sweep) -> pathlib.Path:
    import html
    import shutil
    sweep = pathlib.Path(sweep)
    p = plan(sweep)
    n = len(p["cities"])
    data = json.dumps({k: p[k] for k in ("cities", "model", "regions", "candidates", "tol", "zecrocks_sites")})
    data = data.replace("</", "<\\/")
    m = p["model"]
    out = sweep / "site"
    out.mkdir(parents=True, exist_ok=True)
    assets = pathlib.Path(__file__).parent / "site_assets"
    if not (out / "site.css").exists():
        shutil.copy(assets / "site.css", out / "site.css")
    copy_brand(out)
    E = html.escape
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Region plan · zcash-walletsync-bench</title><meta name="theme-color" content="#171415">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Sora:wght@700;800&family=Manrope:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">
<link rel="icon" href="favicon.svg" type="image/svg+xml"><link rel="stylesheet" href="site.css"><style>{MAP_CSS}</style></head><body class="benchmarks-page">
<header><div class="wrap"><a class="brand" href="#top"><img src="mark.svg" alt="" width="40" height="40"><b>zcash-walletsync-bench</b><span>{E(sweep.name)}</span></a>
<nav><a href="index.html">Sweep results</a><a href="#plan">Plan</a><a href="#method">Method</a></nav></div></header>
<main class="wrap benchmark-report" id="top">
<div class="eyebrow">Geo sweep · region planning</div><h1>Where fastz needs regions to beat zec.rocks</h1>
<p class="sub">Each dot is one of {n} cities measured through Proton VPN, coloured by who answers faster after the VPN hop is
subtracted. <strong>Click the dashed diamonds to add a fastz region there</strong>, or apply the recommended order below; a new
region's latency is predicted from distance. Hover anything for the numbers.</p>
<div class="cards"><div class="card"><span class="who">Today, as DNS steers</span><b id="k-dns">–</b><span>cities where fastz matches or beats zec.rocks</span></div>
<div class="card" style="border-left:1px solid var(--rule);padding-left:1.25rem"><span class="who">Today, best region per city</span><b id="k-best">–</b><span>if DNS always picked the fastest region</span></div>
<div class="card" style="border-left:1px solid var(--rule);padding-left:1.25rem"><span class="who">With the regions on the map</span><b id="k-now">–</b><span id="k-now-sub"></span></div></div>
<div class="controls">
<label>Tie within ± <input id="tol" type="range" min="0" max="10" step="1"><output id="tol-v"></output></label>
<label>New region, same-city latency <input id="floor" type="range" min="2" max="10" step="0.5"><output id="floor-v"></output></label>
<button id="clear">Clear added regions</button></div>
<div class="legend"><span><i style="background:#3987e5"></i>fastz faster</span><span><i style="border:2px solid #d6d0d1"></i>tie</span>
<span><i style="background:#199e70"></i>zec.rocks faster</span><span>dot size: size of the gap</span>
<span>■ fastz region today</span><span>◇ candidate site (click)</span><span style="color:#199e70">△</span><span style="margin-left:-1rem">zec.rocks server nearby (inferred)</span></div>
<div class="mapwrap" id="mapwrap"><svg id="map" role="img" aria-label="World map of the measured cities and fastz regions"></svg><div id="tip"></div></div>
<section id="plan"><h2>Recommended order</h2>
<p>Greedy: each step adds the candidate that wins the most cities with the current settings (ties broken by the total gap).
Click a step to put the first regions of that list on the map. It updates when you move the sliders.</p>
<div class="table-wrap"><table class="plan stats"><thead><tr><th></th><th>Add</th><th>Cities matched or beaten</th><th>Newly won</th></tr></thead>
<tbody id="greedy"></tbody></table></div>
<h3>Cities zec.rocks still wins with the regions on the map <span id="lost-n"></span></h3><ul class="lost" id="lost"></ul></section>
<section id="method"><h2>Method and caveats</h2><ul class="bullets">
<li><strong>For zec.rocks this map shows its cache, not its servers.</strong> zec.rocks answers GetLatestBlock (and tree states
and subtree roots) from a cache layer on Fly in about 14 places, while block streams come from its 5 backend regions
(Amsterdam, New York, São Paulo, Tokyo, and <code>me</code>). So a green dot means a zec.rocks cache machine is closer, and an
added region here is best read as "put a cache or edge replica here", not "add a full node here".</li>
<li><strong>Latency is GetLatestBlock on a warm connection, after the VPN hop.</strong> Per city: the median over 24 rounds to every
fastz region and zec.rocks zone, minus the median round trip to Proton's resolver in that city. zec.rocks is its best zone there.</li>
<li><strong>New regions are predicted, not measured:</strong> {m['a']:.1f} ms + {m['b_per_1000km']:.1f} ms per 1000 km, a robust
fit over the {m['n']} measured (city, fastz region) pairs closer than {m['fit_km']:.0f} km; median error {m['abs_err_p50']:.1f} ms,
90th percentile {m['abs_err_p90']:.1f} ms. The same-city slider replaces the fitted {m['a']:.1f} ms: measured same-city fastz pairs
were 2.4 ms (Frankfurt) and 2.9 ms (Singapore), and zec.rocks reaches 2-5 ms in the cities where it has a server.</li>
<li><strong>Routing matters as much as distance.</strong> From several European exits fastz in Frankfurt is slower than distance
predicts (Berlin: 35 ms after the VPN hop, about 500 km away). Some of that is Proton's routes to OVH; a residential user may see
less of it, and a better-peered provider may help more than another region.</li>
<li>Every city counts the same, and US and European cities are over-represented (Proton's server list). DNS is assumed to send each
city to its fastest region; today it does not in 7 cities (the first card counts that).</li>
<li>Latency is not sync speed. In the sweep's sync runs, fastz tied catch-up up to about 90 ms behind and lost Vizor first sync
even at equal latency; see the <a href="index.html#syncs">sweep page</a>.</li>
<li>Lossy exits (Reykjavik, Oslo, Taichung, Auckland, Johannesburg and others) give noisy values; their tooltips say so.</li></ul></section>
</main><footer><div class="wrap"><a class="by" href="https://zcashlabs.com"><img src="mark.svg" alt="" width="40" height="40">A Zcash Labs project</a><span>Generated by zcash-walletsync-bench from cities.jsonl</span><nav><a href="#top">Top</a></nav></div></footer>
<script id="plan-data" type="application/json">{data}</script>
<script src="https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/topojson-client@3.1.0/dist/topojson-client.min.js"></script>
<script>{MAP_JS}</script></body></html>"""
    path = out / "map.html"
    path.write_text(page)
    return path
