"""Command line: `python -m zcash_walletsync_bench <command>`."""

from __future__ import annotations

import argparse
import json
import os
import sys

from .plan import HARD_WALLET_CAP, RAISED_WALLET_CAP, SUITE_NOTE, parse_levels, parse_sizes
from .scenarios import ORDER
from .services import LOCAL, PRESETS, ZECROCKS_ZONES
from .wallet import PROFILES


def _add_common(p: argparse.ArgumentParser, with_scenarios: bool) -> None:
    if with_scenarios:
        p.add_argument("scenarios", nargs="+", help="one or more of: %s, or 'all'" % ", ".join(ORDER))
    g = p.add_argument_group("services")
    g.add_argument("--services", default="local",
                   help="comma list of: local (default, %s), an --endpoint name, or a preset (%s)"
                   % (LOCAL, ", ".join(sorted(PRESETS))))
    g.add_argument("--zecrocks-zone", default="auto", choices=["auto"] + sorted(ZECROCKS_ZONES),
                   help="zone for 'zecrocks'; auto picks the lowest median GetLightdInfo latency (as the gist does)")
    g.add_argument("--endpoint", action="append", metavar="NAME=HOST:PORT",
                   help="define a service (repeatable). HOST:PORT uses TLS; plain://HOST:PORT uses plaintext. "
                        "NAME can be 'local' to set another address for the local server")
    g.add_argument("--own", action="append", metavar="NAME",
                   help="you operate this --endpoint: permit more than %d wallets on it (repeatable)"
                   % HARD_WALLET_CAP)
    g.add_argument("--ipv4", action="store_true", help="connect over IPv4 only (services differ in IPv6 support)")
    g.add_argument("--resolve", action="append", metavar="HOST=IP",
                   help="connect to this IPv4 address for HOST, like curl --resolve (repeatable)")
    r = p.add_argument_group("repetitions")
    r.add_argument("--reps", type=int, default=None,
                   help="repetitions of every step (default: 10 for short steps, 5 first-sync, 3 per scale level)")
    r.add_argument("--first-sync-reps", type=int, default=5)
    w = p.add_argument_group("workload")
    w.add_argument("--profile", default="vizor",
                   help="wallet(s) whose sync to copy, comma list of: %s" % ", ".join(sorted(PROFILES)))
    w.add_argument("--sizes", type=parse_sizes, default=parse_sizes("1k,10k,50k"),
                   help="first-sync: blocks to restore, comma list (default 1k,10k,50k)")
    w.add_argument("--first-sync-tip", type=int, default=None, help="first-sync: last block (default from fixtures)")
    w.add_argument("--behind", default="1h,1d,1w", type=lambda s: s.split(","),
                   help="catch-up: comma list of absences (1h, 1d, 1w or block counts)")
    w.add_argument("--catch-up-tip", type=int, default=None, help="catch-up: fixed tip instead of the live common tip")
    w.add_argument("--txids", type=int, default=40, help="enhance: transactions per repetition")
    w.add_argument("--full-history-addrs", type=int, default=2, help="transparent: addresses for the full-history search")
    w.add_argument("--duration-s", type=float, default=150.0, help="tip-follow and mempool: seconds to watch")
    w.add_argument("--poll-s", type=float, default=10.0, help="GetLatestBlock poll interval (Vizor app: 10 s)")
    w.add_argument("--observe-s", type=float, default=3.0, help="tip observer interval in tip-follow and soak")
    w.add_argument("--wallets", type=int, default=4, help="mixed: wallets at once")
    w.add_argument("--stagger-s", type=float, default=5.0, help="mixed: wallets start within this many seconds")
    w.add_argument("--seed", type=int, default=1, help="seed for the mixed plan and soak start times")
    w.add_argument("--app-open-samples", type=int, default=2, help="app-open: new connections per repetition")
    w.add_argument("--scale-levels", type=parse_levels, default=parse_levels("1,2,4,8"),
                   help="scale: wallets at once, comma list")
    w.add_argument("--scale-behind", default="1d", help="scale: each wallet's catch-up length")
    w.add_argument("--soak-min", type=float, default=30.0, help="soak: minutes (suite: 0 skips the soak)")
    w.add_argument("--soak-app-open-s", type=float, default=120.0)
    w.add_argument("--soak-catch-up-s", type=float, default=300.0)
    w.add_argument("--soak-enhance-s", type=float, default=180.0)
    w.add_argument("--h2-window", default="wallet", choices=["wallet", "grpc"],
                   help="HTTP/2 receive window: 'wallet' = fixed 2 MiB like tonic/hyper wallets; 'grpc' = grpc-core BDP")
    w.add_argument("--stall-s", type=float, default=2.0, help="a gap between two stream messages above this is a stall")
    pol = p.add_argument_group("politeness")
    pol.add_argument("--max-concurrency", type=int, default=5,
                     help="most block batches in flight per wallet (Vizor's own maximum is 5; at most 8)")
    pol.add_argument("--max-wallets", type=int, default=HARD_WALLET_CAP,
                     help="scale: most wallets at once on any service (at most %d)" % HARD_WALLET_CAP)
    pol.add_argument("--cap", action="append", metavar="NAME=N",
                     help="scale: per-service wallet cap, e.g. --cap local=16 (above %d needs --allow-16-wallets)"
                     % HARD_WALLET_CAP)
    pol.add_argument("--allow-16-wallets", action="store_true",
                     help="raise the scale ceiling from %d to %d wallets (only where --cap allows it)"
                     % (HARD_WALLET_CAP, RAISED_WALLET_CAP))
    pol.add_argument("--pause-s", type=float, default=1.0, help="pause between services in a round")
    o = p.add_argument_group("output")
    o.add_argument("--label", default="run")
    o.add_argument("--out-dir", default="results")
    o.add_argument("--fixtures", default=None, help="fixtures file (default: the mainnet fixtures in the package)")
    o.add_argument("--no-geo", action="store_true", help="do not record the Cloudflare site and country")
    e = p.add_argument_group("plan estimates")
    e.add_argument("--assume-rtt-ms", type=float, default=150.0, help="plan: round trip time for the estimate")
    e.add_argument("--assume-mbps", type=float, default=2.0, help="plan: MB/s per block stream for the estimate")
    e.add_argument("--assume-tip", type=int, default=3_570_000, help="plan: tip height for live-tip steps")
    e.add_argument("--tip-follow-s", type=float, default=600.0, help="suite: tip-follow seconds")


def _add_stress(p: argparse.ArgumentParser) -> None:
    from .stress import DEFAULT_LEVELS, DEFAULT_MIX
    s = p.add_argument_group("stress (own services only above %d wallets)" % HARD_WALLET_CAP)
    s.add_argument("--levels", default=DEFAULT_LEVELS, help="wallets at once per level (default %s)" % DEFAULT_LEVELS)
    s.add_argument("--level-s", type=float, default=60.0, help="seconds each level runs")
    s.add_argument("--stress-sessions", type=int, default=0,
                   help="stop a level after this many sessions in total (0: run for --level-s)")
    s.add_argument("--stress-mix", default=DEFAULT_MIX, help="session mix, kind:weight list")
    s.add_argument("--stress-think-s", type=float, default=1.0, help="mean pause between sessions of a wallet")
    s.add_argument("--stress-first-sync-blocks", type=int, default=1000, help="blocks in a first-sync session")
    s.add_argument("--stress-grace-s", type=float, default=30.0, help="time for running sessions after a level ends")
    s.add_argument("--stress-cooldown-s", type=float, default=10.0, help="pause between levels")
    s.add_argument("--stress-reconnect", action="store_true", help="new connection for every session")
    s.add_argument("--client-procs", type=int, default=max(1, min((os.cpu_count() or 2) // 2, 16)),
                   help="worker processes that drive the wallets (default: half the logical CPUs, at most 16)")
    s.add_argument("--stop-error-rate", type=float, default=0.05, help="stop the ramp above this error rate")
    s.add_argument("--stop-p99-ms", type=float, default=10000.0,
                   help="stop the ramp when unary-call p99 latency over the window is above this")
    s.add_argument("--stop-first-block-p99-ms", type=float, default=20000.0,
                   help="stop the ramp when GetBlockRange first-block p99 is above this (Vizor's stream-start "
                        "deadline is 20 s)")
    s.add_argument("--stop-window-s", type=float, default=15.0, help="window for the p99 thresholds")
    s.add_argument("--stop-min-calls", type=int, default=50, help="calls needed before a threshold can stop a level")
    s.add_argument("--assume-link-mbps", type=float, default=50.0, help="plan: client link in MB/s for the estimate")
    s.add_argument("--yes", action="store_true", help="start a stress run without asking")


def _add_geo(p: argparse.ArgumentParser) -> None:
    p.add_argument("--plan", action="store_true", help="print the cities, the load and the time; send nothing")
    p.add_argument("--phase", default="both", choices=["1", "2", "both"],
                   help="1: latency in every city; 2: syncs in the cities phase 1 picked; both (default)")
    p.add_argument("--sweep-dir", default=None, help="continue this sweep (finished cities are skipped)")
    p.add_argument("--label", default="geo")
    p.add_argument("--out-dir", default="results")
    c = p.add_argument_group("cities")
    c.add_argument("--serverlist", default="~/.cache/Proton/VPN/serverlist.json", help="Proton's cached server list")
    c.add_argument("--max-tier", type=int, default=None, help="server tier (default: the account's)")
    c.add_argument("--cities", default=None, help="only these: country codes, city names or keys, comma separated")
    c.add_argument("--exclude", default=None, help="leave these out (same format)")
    c.add_argument("--thin-km", type=float, default=150.0, help="drop cities closer than this to a kept one")
    c.add_argument("--max-cities", type=int, default=None)
    c.add_argument("--server-tries", type=int, default=2, help="servers to try per city before giving up")
    c.add_argument("--redo", action="store_true", help="measure cities again even if done")
    m = p.add_argument_group("phase 1")
    m.add_argument("--rounds", type=int, default=24, help="rounds per city (even: choose best zone, odd: compare)")
    m.add_argument("--tcp-samples", type=int, default=3)
    m.add_argument("--call-timeout-s", type=float, default=10.0)
    m.add_argument("--margin-ms", type=float, default=1.0, help="a difference inside this is a tie")
    m.add_argument("--seed", type=int, default=1)
    m.add_argument("--assume-rtt-ms", type=float, default=150.0, help="plan: round trip for the time estimate")
    m.add_argument("--allow-accelerator", action="store_true", help="run with Proton's VPN Accelerator on")
    s = p.add_argument_group("phase 2")
    s.add_argument("--sync-cities", type=int, default=8, help="at most this many cities where a competitor won")
    s.add_argument("--controls", type=int, default=2, help="plus this many where fastz won narrowly")
    s.add_argument("--sync-reps", type=int, default=3)
    s.add_argument("--sync-sizes", default="10k", help="first-sync sizes")
    s.add_argument("--sync-behind", default="1d", help="catch-up lengths")
    s.add_argument("--sync-timeout-min", type=float, default=30.0, help="give up on one city's run after this")
    s.add_argument("--repeat-syncs", default=None, metavar="SWEEP_DIR",
                   help="also sync in the phase-2 cities of that earlier sweep (same services and zone) that "
                        "this sweep did not pick, for a city-by-city comparison")
    p.add_argument("--yes", action="store_true", help="start without asking")


def _own_names(a) -> set:
    from .services import OWN
    extra = {e.split("=", 1)[0] for e in (getattr(a, "endpoint", None) or [])}
    return set(OWN) | {n for n in (getattr(a, "own", None) or []) if n in extra}


def _static_services(a):
    """Service objects without any network call (for plan and checks)."""
    from .services import Service
    own = _own_names(a)
    out = []
    for n in [x for x in a.services.split(",") if x]:
        if n not in [s.name for s in out]:
            out.append(Service(n, "dry", own=n in own))
    return out


def _check(ap, a) -> None:
    if any(n > 50_000 for n in a.sizes):
        ap.error("--sizes above 50000 blocks is not allowed")
    if a.max_concurrency > 8:
        ap.error("--max-concurrency above 8 is not polite to third-party services")
    if a.max_wallets > HARD_WALLET_CAP:
        ap.error("--max-wallets is at most %d; use --cap NAME=N with --allow-16-wallets for one service"
                 % HARD_WALLET_CAP)
    own = _own_names(a)
    for item in a.cap or []:
        name, _, n = item.partition("=")
        if int(n) > HARD_WALLET_CAP and name not in own:
            ap.error("--cap %s: only 'local' and --own endpoints can have more than %d wallets"
                     % (item, HARD_WALLET_CAP))
        if int(n) > HARD_WALLET_CAP and not a.allow_16_wallets:
            ap.error("--cap %s needs --allow-16-wallets" % item)
        if int(n) > RAISED_WALLET_CAP:
            ap.error("--cap is at most %d" % RAISED_WALLET_CAP)


def _apply_addresses(ap, a) -> None:
    from .client import ADDRESSES
    ADDRESSES["ipv4"] = bool(getattr(a, "ipv4", False))
    for item in getattr(a, "resolve", None) or []:
        host, _, ip = item.partition("=")
        try:
            import ipaddress
            ipaddress.IPv4Address(ip)
        except ValueError:
            ap.error("--resolve %s: expected HOST=IPv4" % item)
        ADDRESSES["resolve"][host] = ip


def _steps(a, suite: bool):
    from .plan import build_steps, suite_steps
    if suite:
        return suite_steps(a)
    scen = ORDER if a.scenarios == ["all"] else a.scenarios
    for s in scen:
        if s not in ORDER:
            raise SystemExit("unknown scenario %r; choose from %s" % (s, ", ".join(ORDER)))
    return build_steps(a, scen)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="zcash-walletsync-bench", description="Benchmark a lightwalletd-protocol server with the traffic that Zcash wallets send.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    _add_common(sub.add_parser("run", help="run scenarios"), True)
    _add_common(sub.add_parser("suite", help="run the recommended full plan. " + SUITE_NOTE), False)
    pp = sub.add_parser("plan", help="print the request plan and the estimated load per service; sends nothing")
    _add_common(pp, False)
    pp.add_argument("plan_scenarios", nargs="*", help="scenarios (as for run); none = the suite")
    pp.add_argument("--verbose", action="store_true", help="list every request of one repetition")
    _add_stress(pp)
    st = sub.add_parser("stress", help="ramp concurrent wallets on a server you operate (heavy; asks first)")
    _add_common(st, False)
    _add_stress(st)
    sp = sub.add_parser("services", help="resolve services, pick the zec.rocks zone, print GetLightdInfo")
    sp.add_argument("--services", default="local")
    sp.add_argument("--zecrocks-zone", default="auto", choices=["auto"] + sorted(ZECROCKS_ZONES))
    sp.add_argument("--endpoint", action="append", metavar="NAME=HOST:PORT", help="define a service (repeatable)")
    rp = sub.add_parser("report", help="rebuild summary.json, report.md and plots/ from a run directory")
    rp.add_argument("run_dir")
    sp_ = sub.add_parser("site", help="rebuild the site page (<run>/site/index.html) from a run directory")
    sp_.add_argument("run_dir")
    sp_.add_argument("--title", default=None, help="page title (default: own services vs third-party services)")
    fp = sub.add_parser("fixtures", help="rebuild the fixtures file from one service")
    fp.add_argument("--from", dest="source", default="eu.zec.rocks:443")
    fp.add_argument("--window", default="3560000:3560199")
    fp.add_argument("--first-sync-tip", type=int, default=3559999)
    fp.add_argument("--out", default=None)
    sub.add_parser("profiles", help="print the wallet profiles and their sources")
    gs = sub.add_parser("geo-sweep", help="latency from many cities through Proton VPN, then syncs where a "
                        "competitor is closer (reroutes this machine's traffic; asks first)")
    _add_geo(gs)
    gr = sub.add_parser("geo-report", help="rebuild the summary of a geo sweep directory")
    gr.add_argument("sweep_dir")
    gw = sub.add_parser("geo-site", help="rebuild the page of a geo sweep (<sweep>/site/index.html)")
    gw.add_argument("sweep_dir")
    gm = sub.add_parser("geo-map", help="region-planning map of a geo sweep (<sweep>/site/map.html)")
    gm.add_argument("sweep_dir")
    gc = sub.add_parser("geo-compare", help="compare a geo sweep with an earlier one run the same way "
                        "(<new>/compare.txt, compare.json, site/compare.html)")
    gc.add_argument("old_dir")
    gc.add_argument("new_dir")
    a = ap.parse_args(argv)

    if a.cmd in ("run", "suite"):
        _check(ap, a)
        _apply_addresses(ap, a)
        from .client import H2_WINDOW
        from .runner import run_plan
        H2_WINDOW["mode"] = a.h2_window
        steps = _steps(a, a.cmd == "suite")
        from . import fixtures as fx
        from .plan import estimate, print_plan
        from .services import Service
        names = [n for n in a.services.split(",") if n]
        est = estimate(a, steps, fx.load(a.fixtures), [Service(n, "dry") for n in names], a.assume_tip)
        print(print_plan(est, names), flush=True)
        run_plan(a, steps)
        return 0
    if a.cmd in ("stress",) or (a.cmd == "plan" and a.plan_scenarios == ["stress"]):
        _check(ap, a)
        from . import fixtures as fx
        from .plan import estimate_stress, print_stress_plan, stress_steps
        try:
            steps = stress_steps(a, _static_services(a))
        except ValueError as exc:
            ap.error(str(exc))
        est = estimate_stress(a, steps, fx.load(a.fixtures), a.assume_tip)
        print(print_stress_plan(est, a), flush=True)
        if a.cmd == "plan":
            return 0
        if not a.yes:
            if not sys.stdin.isatty():
                ap.error("a stress run is heavy: pass --yes, or run it from a terminal and confirm")
            if input("Start this stress run? Type yes: ").strip().lower() != "yes":
                print("Not started.")
                return 1
        _apply_addresses(ap, a)
        from .client import H2_WINDOW
        from .runner import run_plan
        H2_WINDOW["mode"] = a.h2_window
        run_plan(a, steps)
        return 0
    if a.cmd == "plan":
        _check(ap, a)
        from . import fixtures as fx
        from .plan import estimate, print_plan
        from .services import Service
        a.scenarios = a.plan_scenarios
        steps = _steps(a, not a.plan_scenarios)
        names = [n for n in a.services.split(",") if n]
        est = estimate(a, steps, fx.load(a.fixtures), [Service(n, "dry") for n in names], a.assume_tip)
        print(SUITE_NOTE if not a.plan_scenarios else "Plan for: %s" % ", ".join(a.plan_scenarios))
        print(print_plan(est, names, verbose=a.verbose))
        return 0
    if a.cmd == "services":
        from .services import describe_error, lightd_info, resolve
        extra = dict(e.split("=", 1) for e in (a.endpoint or []))
        svcs, meta = resolve([n for n in a.services.split(",") if n], a.zecrocks_zone, extra)
        print(json.dumps(meta, indent=1))
        for s in svcs:
            try:
                print(s.name, s.endpoint, json.dumps(lightd_info(s.endpoint)))
            except Exception as exc:
                print(s.name, s.endpoint, "ERROR", describe_error(exc))
        return 0
    if a.cmd == "report":
        from .analysis import build
        build(a.run_dir)
        print("rebuilt %s/report.md, summary.json and plots/" % a.run_dir.rstrip("/"))
        try:
            from .site import build_site
            print("rebuilt", build_site(a.run_dir))
        except ImportError as exc:  # matplotlib missing
            print("site not rebuilt:", exc)
        return 0
    if a.cmd == "site":
        from .site import build_site
        print("wrote", build_site(a.run_dir, a.title))
        return 0
    if a.cmd == "fixtures":
        from .fixtures import DEFAULT_PATH, build_fixtures
        lo, hi = (int(x) for x in a.window.split(":"))
        f = build_fixtures(a.source, lo, hi, a.first_sync_tip)
        out = a.out or DEFAULT_PATH
        with open(out, "w") as fh:
            json.dump(f, fh, indent=1)
            fh.write("\n")
        print("wrote", out)
        return 0
    if a.cmd == "geo-sweep":
        from .geosweep import main as geo_main
        return geo_main(a)
    if a.cmd == "geo-report":
        from .georeport import build
        print(build(a.sweep_dir))
        try:
            from .geosite import build_geo_site
            print("wrote", build_geo_site(a.sweep_dir))
        except ImportError as exc:  # matplotlib missing
            print("site not rebuilt:", exc)
        return 0
    if a.cmd == "geo-map":
        from .geoplan import build_map, plan, summary_text
        print(summary_text(plan(a.sweep_dir)))
        print("wrote", build_map(a.sweep_dir))
        return 0
    if a.cmd == "geo-compare":
        from .geocompare import build as compare_build
        print(compare_build(a.old_dir, a.new_dir))
        return 0
    if a.cmd == "geo-site":
        from .geosite import build_geo_site
        print("wrote", build_geo_site(a.sweep_dir))
        return 0
    if a.cmd == "profiles":
        for p in PROFILES.values():
            print("%s: batch %d blocks, %d in flight, tree state %s, stream deadline %ss, deadlines %s" % (
                p.name, p.batch_blocks, p.inflight, p.tree_state, p.stream_deadline_s, p.deadlines))
            for s in p.sources:
                print("   ", s)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
