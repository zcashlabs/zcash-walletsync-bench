"""Summary of a geo sweep: a text table (returned) and <sweep>/summary.json.

Latency columns are medians over the measuring rounds in one city. The meaningful numbers are the
differences between hosts in the same city (delta: competitor minus fastz, paired by round, with a
bootstrap 95% CI); the absolute values include this machine's trip to the VPN server.
"""

from __future__ import annotations

import json
import pathlib

from .geosweep import best_records, default_targets, latest_ok, read_jsonl, summarize_city


def _delta(v: dict | None) -> str:
    if not v or not v.get("n"):
        return "-"
    ci = v.get("ci95_ms")
    mark = {"faster": " <", "slower": "", "tie": " ="}.get(v.get("verdict"), " ?")
    return "%+.1f%s%s" % (v["median_ms"], " [%+.1f,%+.1f]" % tuple(ci) if ci else "", mark)


def _wall(run_dir: pathlib.Path) -> dict:
    """{step: {service: wall_s p50}} from a phase-2 run's summary.json."""
    p = run_dir / "summary.json"
    if not p.exists():
        return {}
    s = json.loads(p.read_text())
    out = {}
    for sid, st in s.get("steps", {}).items():
        if st.get("scenario") not in ("catch-up", "first-sync"):
            continue
        out[sid] = {svc: (ps.get("rep_stats", {}).get("wall_s") or {}).get("p50")
                    for svc, ps in st.get("per_service", {}).items()}
        cmp_ = (st.get("compare") or {}).get("wall_s") or {}
        out[sid]["_best"] = cmp_.get("best")
        out[sid]["_vs_best"] = {k: {"ratio": v.get("ratio"), "significant": v.get("significant")}
                                for k, v in (cmp_.get("vs_best") or {}).items()}
    return out


def build(sweep) -> str:
    sweep = pathlib.Path(sweep)
    targets = default_targets()
    meta = json.loads((sweep / "sweep.json").read_text()) if (sweep / "sweep.json").exists() else {}
    sessions = meta.get("sessions") or [{}]
    margin = float((sessions[-1].get("options") or {}).get("margin_ms", 1.0))
    all_recs = read_jsonl(sweep / "cities.jsonl")
    recs = best_records(all_recs)
    failed = sorted({r["city"] for r in all_recs if not r.get("ok")} - set(recs))
    rows, lines = [], []
    lines.append("Geo sweep %s: %d cities measured%s" % (sweep.name, len(recs),
                                                         ", %d failed (%s)" % (len(failed), ", ".join(failed))
                                                         if failed else ""))
    lines.append("delta = competitor minus fastz (DNS-steered) in ms, paired by round, 95% CI; "
                 "'<' competitor faster, '=' tie, plain: fastz faster.")
    lines.append("")
    hdr = "%-26s %5s %6s %-11s %8s %-10s %8s %-22s %-24s %-24s %s" % (
        "city", "exit", "tunnel", "fastz dns", "p50", "best rgn", "p50", "zec.rocks best", "delta zec.rocks",
        "delta zcashlabs", "")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for key, r in sorted(recs.items(), key=lambda kv: (kv[1].get("country", ""), kv[0])):
        s = summarize_city(r, targets, margin)
        if not s.get("ok"):
            continue
        t = s["targets"]
        zr = s["competitors"].get("zecrocks", {})
        zl = s["competitors"].get("zcashlabs", {})
        fb = s.get("fastz_best")
        dns_rgn = r.get("fastz_dns_region") or "?"
        steer = s.get("steering") or {}
        flag = "" if not fb or dns_rgn == fb[len("fastz-"):] else " (!)"
        lines.append("%-26s %5s %6.0f %-11s %8s %-10s %8s %-22s %-24s %-24s %s" % (
            ("%s, %s" % (r.get("name"), r.get("country")))[:26], (r.get("exit") or {}).get("colo") or "?",
            s["tunnel_p50_ms"] or 0, dns_rgn + flag, t.get("fastz", {}).get("p50", "-"),
            (fb or "-")[len("fastz-"):] if fb else "-", t.get(fb, {}).get("p50", "-") if fb else "-",
            "%s %s" % (zr.get("target", "-")[len("zecrocks-"):], t.get(zr.get("target"), {}).get("p50", "")),
            _delta(zr.get("vs_fastz")), _delta(zl.get("vs_fastz")), "phase 2" if s["qualifies"] else ""))
        rows.append({"city": key, "country": r.get("country"), "name": r.get("name"), "lat": r.get("lat"),
                     "lon": r.get("lon"), "server": r.get("server"), "exit": r.get("exit"),
                     "resolver": r.get("resolver"), "fastz_dns_region": r.get("fastz_dns_region"),
                     "steering": steer, "summary": s})
    mis = [x for x in rows if (x["steering"] or {}).get("misteered")]
    if mis:
        lines += ["", "DNS chose a slower fastz region than the best one in: " + ", ".join(
            "%s (%s instead of %s, %+.1f ms)" % (x["name"], x["steering"]["dns_region"],
                                                 x["steering"]["best_region"],
                                                 x["steering"]["dns_vs_best"]["median_ms"]) for x in mis)]
    off = [x for x in rows if (x.get("resolver") or {}).get("same_net_as_exit") is False]
    if off:
        lines += ["", "Proton's resolver asked from another network than the exit in: " +
                  ", ".join(x["name"] for x in off) + " (DNS steering there reflects the resolver's location)"]

    syncs = latest_ok(read_jsonl(sweep / "syncs.jsonl"))
    sync_rows = []
    if syncs:
        lines += ["", "Phase 2 wallet syncs (median wall time, s; ratio = service / fastz)", ""]
        for key, sy in sorted(syncs.items()):
            w = _wall(sweep / sy["run_dir"]) if sy.get("run_dir") else {}
            sync_rows.append({"city": key, "why": sy.get("why"), "services": sy.get("services"),
                              "run_dir": sy.get("run_dir"), "wall_s": w})
            for sid, per in sorted(w.items()):
                base = per.get("fastz")
                cells = []
                for svc in sy.get("services", []):
                    v = per.get(svc)
                    if v is None:
                        continue
                    ratio = "" if svc == "fastz" or not base else " (x%.2f)" % (v / base)
                    cells.append("%s %.2f%s" % (svc, v, ratio))
                lines.append("  %-20s %-18s %-28s %s" % (key, sy.get("why", ""), sid, ", ".join(cells)))
    (sweep / "summary.json").write_text(json.dumps({"cities": rows, "failed": failed, "syncs": sync_rows},
                                                   indent=1, default=str))
    lines += ["", "wrote %s" % (sweep / "summary.json")]
    return "\n".join(lines)
