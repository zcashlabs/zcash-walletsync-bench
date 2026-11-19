"""Markdown report from run.json and summary.json (see analysis.py)."""

from __future__ import annotations

STAT_COLS = ["n", "mean", "stdev", "min", "p5", "p25", "p50", "p75", "p90", "p95", "p99", "max"]

UNITS = {"wall_s": "s", "time_to_first_block_s": "s", "mbytes_per_s": "MB/s", "blocks_per_s": "blocks/s",
         "prelude_s": "s", "blocks_phase_s": "s", "tail_s": "s", "tx_per_s": "tx/s", "first_rpc_ms": "ms",
         "grpc_connect_ms": "ms", "aggregate_mbytes_per_s": "MB/s", "wallet_mbytes_per_s": "MB/s",
         "wallet_wall_s": "s", "history_s": "s", "full_history_s": "s", "utxo_s": "s",
         "block_arrival_delay_s": "s", "stream_resumes": "count", "client_parse_s": "s"}
REP_ORDER = ["wall_s", "time_to_first_block_s", "mbytes_per_s", "blocks_per_s", "prelude_s", "blocks_phase_s",
             "tail_s", "stream_resumes", "tx_per_s", "utxo_s", "history_s", "full_history_s", "first_rpc_ms",
             "grpc_connect_ms", "aggregate_mbytes_per_s", "wallet_mbytes_per_s", "wallet_wall_s",
             "block_arrival_delay_s"]


def _f(v, nd=2):
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "NO"
    if isinstance(v, float):
        if abs(v) >= 1000:
            return "%.0f" % v
        if abs(v) < 1:
            return "%.3f" % v
        return ("%%.%df" % nd) % v
    return str(v)


def _table(head, rows) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(c if isinstance(c, str) else _f(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _ci(d):
    ci = d.get("median_ci95")
    return "[%s, %s]" % (_f(ci[0]), _f(ci[1])) if ci else "-"


def _stat_row(label, d):
    return [label] + [d.get(k) for k in STAT_COLS] + [_ci(d)]


def _stat_table(first_col, rows) -> str:
    return _table([first_col] + STAT_COLS + ["median 95% CI"], [_stat_row(l, d) for l, d in rows if d.get("n")])


def _comparison(comp: dict) -> str:
    rows = []
    for metric, c in comp.items():
        if not c:
            continue
        for s, r in c["vs_best"].items():
            if r.get("ratio") is None or not r.get("ci95"):
                verdict = r.get("note", "no CI")
            elif r["significant"]:
                verdict = "different (95% CI excludes 1)"
            else:
                verdict = "not significant"
            rows.append([metric, c["best"], s, r.get("ratio"),
                         "[%s, %s]" % tuple(_f(x, 3) for x in r["ci95"]) if r.get("ci95") else "-",
                         r.get("p_mannwhitney"), verdict])
    if not rows:
        return ""
    return _table(["metric", "best (median)", "service", "median ratio vs best", "95% CI", "Mann-Whitney p",
                   "verdict"], rows) + (
        "\n\nRatio = median of the service / median of the best. For latency and time, above 1 is slower. "
        "For MB/s and tx/s, below 1 is slower. 'Not significant' means the bootstrap 95% CI of the ratio "
        "contains 1, so this run cannot tell the two apart.")


def render(run: dict, summary: dict) -> str:
    L = []
    meta = run["meta"]
    loc = meta.get("location") or {}
    plots = summary.get("plots") or {}
    L.append("# zcash-walletsync-bench: %s" % run["label"])
    L.append("")
    L.append("Started %s, ended %s (UTC). Python %s, grpcio %s, %s %s. Client location: Cloudflare site %s, "
             "country %s. HTTP/2 window: %s. Fixtures %s (sha256 %s)." % (
                 run.get("started_utc"), run.get("ended_utc"), meta["python"], meta["grpcio"], meta["os"],
                 meta["machine"], loc.get("cloudflare_site"), loc.get("country"), run.get("h2_window"),
                 run["fixtures_file"]["name"], run["fixtures_file"]["sha256"][:12]))
    sel = run.get("selection") or {}
    if sel.get("zecrocks_zone"):
        L.append("")
        L.append("zec.rocks zone: %s (median GetLightdInfo ms per zone: %s)." % (
            sel["zecrocks_zone"], sel.get("zecrocks_zone_latency_ms", "chosen by option")))
    L.append("")
    L.append("Raw data: `raw/calls.jsonl.gz` (one line per call), `raw/messages.csv.gz` (one row per stream "
             "message), `raw/connections.jsonl.gz`, `raw/runs.jsonl.gz`, `raw/tips.jsonl.gz`. Statistics: "
             "`summary.json`. Rebuild with `zcash-walletsync-bench report <this directory>`.")
    if run.get("aborted"):
        L.append("")
        L.append("**Stopped early:** %s" % run["aborted"])

    L.append("")
    L.append("## Services")
    L.append("")
    rows = []
    for s in run["services"]:
        i, c = s.get("lightd_info", {}), s["connection"]
        rows.append([s["name"], "own" if s.get("own") else "third-party", s["endpoint"],
                     "%s %s" % (i.get("vendor", "?"), i.get("version", "")),
                     i.get("node_subversion", s.get("lightd_info_error", "?")), c["dns_ms_p50"],
                     c["tcp_connect_ms_p50"], c.get("tcp_connect_ms_min"), c["tls_handshake_ms_p50"]])
    L.append(_table(["service", "kind", "endpoint", "server (GetLightdInfo)", "node", "DNS ms p50", "TCP ms p50",
                     "TCP ms min (~RTT)", "TLS ms p50"], rows))
    L.append("")
    L.append("A call cannot take less than one round trip (RTT). TCP connect time is close to one RTT. The part "
             "of a latency above the RTT is the server, its proxy, or its upstream node.")

    tips = summary.get("tips") or {}
    if tips.get("per_service"):
        L.append("")
        L.append("## Tip freshness")
        L.append("")
        L.append("GetLatestBlock to all services at the same moment. Lag is in blocks behind the highest answer.")
        L.append("")
        L.append(_table(["service", "snapshots", "behind (fraction)", "lag mean", "lag p95", "lag max"],
                        [[s, v["snapshots"], v["behind_fraction"], v["lag_blocks"].get("mean"),
                          v["lag_blocks"].get("p95"), v["lag_blocks"].get("max")]
                         for s, v in tips["per_service"].items()]))
        ad = tips.get("arrival_delay_s") or {}
        if any(v.get("n") for v in ad.values()):
            L.append("")
            L.append("New-block arrival delay: seconds after the first service reported a height (tip-follow and "
                     "soak snapshots only; resolution = the observer interval).")
            L.append("")
            L.append(_stat_table("service", list(ad.items())))
        for p in plots.get("tip_lag", []):
            L.append("")
            L.append("![tip lag](%s)" % p)

    if summary.get("stress"):
        L.append("")
        L.append(_stress_section(run, summary["stress"], plots))

    if summary.get("scale"):
        L.append("")
        L.append("## Load curve (scale)")
        for prof, per in summary["scale"].items():
            L.append("")
            L.append("Profile %s. Each wallet catches up on its own connection; all wallets start together." % prof)
            L.append("")
            rows = []
            for s, levels in per.items():
                for w in sorted(levels, key=int):
                    v = levels[w]
                    rows.append([s, w, v["aggregate_mbytes_per_s"].get("p50"), _ci(v["aggregate_mbytes_per_s"]),
                                 v["wallet_mbytes_per_s"].get("p50"), v["wallet_mbytes_per_s"].get("p5"),
                                 v["wall_s"].get("p50"), v["first_block_ms"].get("p50"),
                                 v["first_block_ms"].get("p95"), v["calls"], v["errors"]])
            L.append(_table(["service", "wallets", "aggregate MB/s p50", "95% CI", "per-wallet MB/s p50",
                             "per-wallet MB/s p5", "wall s p50", "first block ms p50", "first block ms p95",
                             "calls", "errors"], rows))
        for p in plots.get("load_curve", []):
            L.append("")
            L.append("![load curve](%s)" % p)

    if summary.get("soak"):
        L.append("")
        L.append("## Soak (per minute)")
        L.append("")
        rows = []
        for s, mins in summary["soak"].items():
            calls = sum(r["calls"] for r in mins)
            errs = sum(r["errors"] for r in mins)
            p50s = [r["poll_p50_ms"] for r in mins if r["poll_p50_ms"] is not None]
            p95s = [r["poll_p95_ms"] for r in mins if r["poll_p95_ms"] is not None]
            worst = max(mins, key=lambda r: r["errors"]) if mins else {}
            rows.append([s, len(mins), calls, errs, round(errs / calls, 4) if calls else None,
                         min(p50s) if p50s else None, max(p50s) if p50s else None, max(p95s) if p95s else None,
                         "%s (%s errors)" % (worst.get("minute"), worst.get("errors")) if worst else "-"])
        L.append(_table(["service", "minutes", "calls", "errors", "error rate", "poll p50 ms best minute",
                         "poll p50 ms worst minute", "poll p95 ms worst minute", "minute with most errors"], rows))
        L.append("")
        L.append("Every minute is in `summary.json` under `soak`.")
        for p in plots.get("soak", []):
            L.append("")
            L.append("![soak](%s)" % p)

    for sid, st in summary["steps"].items():
        L.append("")
        L.append("## %s" % sid)
        L.append("")
        L.append("Scenario %s, %d repetition(s) per service, service order rotated each repetition. Parameters: "
                 "%s." % (st["scenario"], st["reps"], st["params"]))
        per = st["per_service"]
        failed = {s: v["reps_failed"] for s, v in per.items() if v["reps_failed"]}
        if failed:
            L.append("")
            L.append("Failed repetitions: %s. Examples: %s" % (failed, {s: v["rep_errors"][:2] for s, v in
                                                                         per.items() if v["rep_errors"]}))
        # Per-repetition metrics.
        metrics = [m for m in REP_ORDER if any(m in v["rep_stats"] for v in per.values())]
        for m in metrics:
            rows = [(s, v["rep_stats"].get(m, {"n": 0})) for s, v in per.items()]
            if any(d.get("n") for _, d in rows):
                L.append("")
                L.append("**%s** (%s; one value per repetition%s):" % (
                    m, UNITS.get(m, ""), "" if m not in ("first_rpc_ms", "grpc_connect_ms", "wallet_mbytes_per_s",
                                                         "wallet_wall_s", "block_arrival_delay_s")
                    else ", or per connection / wallet / block"))
                L.append("")
                L.append(_stat_table("service", rows))
        if st["compare"]:
            L.append("")
            L.append("**Comparison**")
            L.append("")
            L.append(_comparison(st["compare"]))
        # Per-method latency.
        rows = []
        for s, v in per.items():
            for meth, d in v["methods"].items():
                if d["latency_ms"].get("n"):
                    rows.append(("%s %s" % (s, meth), d["latency_ms"]))
        if rows:
            L.append("")
            L.append("**Call latency, ms** (warm calls: not the first call on a connection):")
            L.append("")
            L.append(_stat_table("service method", rows))
        # Streams.
        rows = []
        for s, v in per.items():
            b = v.get("block_streams")
            if b:
                rows.append([s, b["streams"], b["first_message_ms"].get("p50"), b["first_message_ms"].get("p95"),
                             b["transfer_mbytes_per_s"].get("p50"), _ci(b["transfer_mbytes_per_s"]),
                             b["bucket_1s_mbytes_per_s"].get("p5"), b["bucket_1s_mbytes_per_s"].get("p50"),
                             b["bucket_1s_mbytes_per_s"].get("p95"), b["bucket_1s_blocks_per_s"].get("p50"),
                             b["max_gap_ms"].get("p99"), b["stalls"], b["stall_s"]])
        if rows:
            L.append("")
            L.append("**GetBlockRange streams** (transfer = bytes / (stream time - first-block time); 1 s buckets "
                     "per stream; a stall is a gap between two blocks above %s s):" % (
                         run["options"].get("stall_s", 2.0)))
            L.append("")
            L.append(_table(["service", "streams", "first block ms p50", "p95", "transfer MB/s p50", "95% CI",
                             "1 s bucket MB/s p5", "p50", "p95", "blocks/s per 1 s p50", "max gap ms p99",
                             "stalls", "stall s"], rows))
        # Errors.
        rows = []
        for s, v in per.items():
            for meth, d in v["methods"].items():
                rows.append([s, meth, d["calls"], d["errors"], d["error_rate"],
                             ", ".join("%s x%d" % kv for kv in sorted(d["errors_by_status"].items())) or "-"])
        if rows:
            L.append("")
            L.append("**Errors**")
            L.append("")
            L.append(_table(["service", "method", "calls", "errors", "error rate", "by status"], rows))
        # Deadlines.
        rows = []
        for s, v in per.items():
            for prof, hits in v["deadlines"].items():
                for name, (n, tot) in hits.items():
                    rows.append([s, prof, name, n, tot, round(n / tot, 4) if tot else None])
            if v["retries"]:
                rows.append([s, "(this run)", "streams re-opened after a deadline", v["retries"], "-", "-"])
        if rows:
            L.append("")
            L.append("**Wallet deadlines**: how many calls would have hit each wallet's own client deadline "
                     "(Vizor `lwd.rs:50-53`, Zingo `zingo-netutils/src/time.rs:170-178`; librustzcash sets none).")
            L.append("")
            L.append(_table(["service", "wallet", "deadline", "calls over it", "calls checked", "fraction"], rows))
        cc = st.get("crosscheck")
        if cc and cc.get("kinds"):
            parts = []
            for k, v in cc["kinds"].items():
                p = "%s %d compared, %d mismatched" % (k, v["compared"], v["mismatches"])
                if v.get("aux_differences"):
                    p += " (%d aux-only differences)" % v["aux_differences"]
                if v.get("completing_hash_order"):
                    p += " [completingBlockHash byte order: %s]" % ", ".join(
                        "%s=%s" % kv for kv in v["completing_hash_order"].items())
                parts.append(p)
            L.append("")
            L.append("Correctness: **%s**. %s.%s%s" % (
                "match" if cc["ok"] else "MISMATCH", "; ".join(parts),
                (" Odd one out: %s." % cc["odd_one_out"]) if cc.get("odd_one_out") else "",
                (" Changed between repetitions: %s." % cc["unstable_between_reps"][:5])
                if cc.get("unstable_between_reps") else ""))
        for kind in ("cdf", "throughput"):
            for p in plots.get(kind, []):
                if p.endswith("-%s.png" % _safe(sid)):
                    L.append("")
                    L.append("![%s](%s)" % (kind, p))
    L.append("")
    return "\n".join(L)


def _safe(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def _stress_section(run, st: dict, plots: dict) -> str:
    L = ["## Stress", ""]
    o = run["options"]
    L.append("Wallets ramp up level by level; each level runs %s s. Each wallet runs sessions from the mix %s "
             "with a mean think time of %s s. The ramp stops when the error rate is above %s, unary p99 is above "
             "%s ms, or GetBlockRange first-block p99 is above %s ms (over the last %s s)." % (
                 o.get("level_s"), o.get("stress_mix"), o.get("stress_think_s"), o.get("stop_error_rate"),
                 o.get("stop_p99_ms"), o.get("stop_first_block_p99_ms"), o.get("stop_window_s")))
    for svc_name, v in st.items():
        L.append("")
        L.append("### %s" % svc_name)
        L.append("")
        rows = []
        for e in v["levels"]:
            c = e["client"] or {}
            dl = []
            for prof, hits in (e["deadlines"] or {}).items():
                for name, (n, tot) in hits.items():
                    if n:
                        dl.append("%s %s: %d/%d" % (prof, name, n, tot))
            rows.append([e["wallets"], e["active_s"], "%d (%d ok)" % (e["sessions"], e["sessions_ok"]),
                         e["requests_per_s"], e["aggregate_mbytes_per_s"], e["wallet_mbytes_per_s"].get("p5"),
                         e["wallet_mbytes_per_s"].get("p50"), e["unary_ms"].get("p50"), e["unary_ms"].get("p90"),
                         e["unary_ms"].get("p99"), e["first_block_ms"].get("p50"), e["first_block_ms"].get("p99"),
                         e["error_rate"], ", ".join("%s x%d" % kv for kv in sorted(e["errors_by_status"].items()))
                         or "-", "; ".join(dl) or "-", c.get("cpu_pct_max_proc"), c.get("system_cpu_pct_mean"),
                         c.get("sched_delay_p99_ms_max"), "YES" if c.get("client_limited") else "no",
                         e["stop_reason"] or "-"])
        L.append(_table(["wallets", "s", "sessions", "req/s", "MB/s", "wallet MB/s p5", "wallet MB/s p50",
                         "unary p50 ms", "p90", "p99", "first block p50 ms", "p99", "error rate", "errors",
                         "wallet deadlines hit", "client CPU busiest worker %", "client machine CPU %",
                         "client sched delay p99 ms", "client-limited", "stopped because"], rows))
        k = v.get("knee") or {}
        L.append("")
        L.append("Knee estimate: **%s** (%s).%s" % (
            "%s wallets" % k["level"] if k.get("level") else "not reached", k.get("reason"),
            " " + k["note"] if k.get("note") else ""))
        rows = []
        for e in v["levels"]:
            for m, d in sorted(e["methods"].items()):
                rows.append([e["wallets"], m, d["calls"], d["errors"], d["p50"], d["p90"], d["p99"]])
        if rows:
            L.append("")
            L.append("Latency per method and level (ms, successful calls):")
            L.append("")
            L.append(_table(["wallets", "method", "calls", "errors", "p50", "p90", "p99"], rows))
    L.append("")
    L.append("Client-limited means the busiest worker process used at least 90% of a core, the 20 ms "
             "scheduling probe woke up at least 100 ms late at p99, or the whole machine was at least 90% busy. "
             "Then the numbers at that level show the client's limit, not the server's. Use more "
             "`--client-procs` or a bigger machine.")
    for p in plots.get("stress", []):
        L.append("")
        L.append("![stress](%s)" % p)
    return "\n".join(L)
