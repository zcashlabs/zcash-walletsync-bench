"""Run a plan of steps against the services and write the raw data.

A plan is a list of steps. A step is one scenario with fixed parameters (for
example first-sync, profile vizor, 10,000 blocks) and a number of
repetitions. Each repetition runs every service once, in an order that is
rotated per repetition, so slow drift in the network affects every service
the same way. Every call, stream message, connection, repetition and tip
snapshot is written to the run directory (see rawio.py). The report is then
computed from those files only.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
import traceback
import types
from datetime import datetime, timezone

from . import envmeta, fixtures as fx_mod
from .client import H2_WINDOW, RateLimited, Recorder, is_loopback, probe_connection
from .crosscheck import compare
from .freshness import TipObserver
from .rawio import RawWriter
from .scenarios import ALL_SERVICES, PER_SERVICE, Ctx, mempool, soak, tip_follow
from .services import describe_error, lightd_info, resolve
from .wallet import PROFILES

SCHEMA = "zcash-walletsync-bench/2"


def log(msg: str) -> None:
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def scrub(text):
    """Remove the home directory (it holds the username) from strings."""
    if not isinstance(text, str):
        return text
    home = os.path.expanduser("~")
    return text.replace(home, "~") if home and home != "/" else text


def options_record(opts) -> dict:
    out = {}
    for k, v in sorted(vars(opts).items()):
        if k.startswith("_") or callable(v):
            continue
        if k in ("out_dir", "fixtures") and isinstance(v, str):
            v = os.path.basename(v.rstrip("/")) or v
        out[k] = v
    return out


def step_opts(opts, overrides: dict):
    d = dict(vars(opts))
    d.update(overrides)
    return types.SimpleNamespace(**d)


def merge_digests(into: dict, new: dict, unstable: list, service: str, rep: int) -> None:
    """Merge one repetition's digests. A key whose digest changes between
    repetitions of the same service is recorded as unstable."""
    for kind, d in new.items():
        tgt = into.setdefault(kind, {})
        for key, dig in d.items():
            if dig is None:
                continue
            old = tgt.get(key)
            if old is not None and old.get("core") != dig.get("core"):
                if len(unstable) < 50:
                    unstable.append({"service": service, "kind": kind, "key": str(key), "rep": rep})
            else:
                tgt[key] = dig


def run_plan(opts, steps: list[dict]) -> pathlib.Path:
    extra = dict(e.split("=", 1) for e in (opts.endpoint or []))
    names = [n for n in opts.services.split(",") if n]
    log("Resolving services %s" % ", ".join(names))
    services, selection = resolve(names, opts.zecrocks_zone, extra, set(getattr(opts, "own", None) or []))
    fixtures = fx_mod.load(opts.fixtures)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = pathlib.Path(opts.out_dir) / ("%s-%s" % (opts.label, stamp))
    run_dir.mkdir(parents=True, exist_ok=True)
    sink = RawWriter(run_dir)
    rec = Recorder(sink=sink, stall_s=opts.stall_s)
    meta = {
        "schema": SCHEMA,
        "label": opts.label,
        "meta": envmeta.collect(geo=not opts.no_geo and any(not is_loopback(x.endpoint) for x in services)),
        "options": options_record(opts),
        "h2_window": H2_WINDOW["mode"],
        "profiles": {n: {"sources": list(p.sources), "deadlines": p.deadlines,
                         **{k: v for k, v in vars(p).items() if k not in ("sources", "name", "deadlines")}}
                     for n, p in PROFILES.items()},
        "selection": selection,
        "fixtures": {k: v for k, v in fixtures.items() if not k.startswith("_")},
        "fixtures_file": {"name": fixtures["_path"], "sha256": fixtures["_sha256"]},
        "services": [],
        "steps": steps,
        "aborted": {},
        "started_utc": stamp,
    }
    for s in services:
        log("Probing %s (%s)" % (s.name, s.endpoint))
        entry = {"name": s.name, "endpoint": s.endpoint, "note": s.note, "own": s.own, "kind": s.kind}
        try:
            entry["lightd_info"] = lightd_info(s.endpoint)
        except Exception as exc:
            entry["lightd_info_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            log("  no answer from %s: %s" % (s.name, describe_error(exc)))
        entry["connection"] = probe_connection(s.endpoint, samples=5)
        meta["services"].append(entry)
    live = [s for s, e in zip(services, meta["services"]) if "lightd_info" in e]
    if not live:
        _write_json(run_dir / "run.json", meta)
        hint = ""
        if any(s.name == "local" for s in services):
            hint = (" Start your server, or give its address: --endpoint local=plain://HOST:PORT"
                    " (plaintext) or --endpoint local=HOST:PORT (TLS).")
        raise SystemExit("No service answered GetLightdInfo. Nothing was measured.%s" % hint)
    chains = {e["lightd_info"]["chain_name"] for e in meta["services"] if "lightd_info" in e}
    if len(chains) > 1:
        raise SystemExit("services are on different chains: %s" % chains)
    _write_json(run_dir / "run.json", meta)

    run_epoch_wall = time.time() - rec.now()
    observer = TipObserver(live, rec)
    crosschecks: dict = {}
    round_no = 0
    try:
        for k, step in enumerate(steps):
            active = [s for s in live if s.name not in meta["aborted"]]
            if not active:
                break
            o = step_opts(opts, step["overrides"])
            scen = step["scenario"]
            snap = observer.snapshot("%s:start" % step["id"])
            tips = [h for n, h in snap["heights"].items() if h is not None and n in {s.name for s in active}]
            if not tips:
                log("No service answered GetLatestBlock; stopping.")
                break
            common_tip = min(tips)
            log("Step %d/%d %s: %d repetition(s), common tip %d" % (k + 1, len(steps), step["id"], step["reps"],
                                                                  common_tip))
            if scen == "stress":
                from .stress import run_ramp
                target = next((s for s in active if s.name == step["overrides"]["service"]), None)
                if target is None:
                    log("  %s: service not available; skipped" % step["id"])
                    continue
                run_ramp(target, step, o, fixtures, observer, sink, run_dir, run_epoch_wall, log)
                crosschecks[step["id"]] = {"ok": True, "kinds": {}, "odd_one_out": {},
                                           "note": "stress steps are not cross-checked"}
                continue
            digests: dict[str, dict] = {}
            unstable: list = []
            for rep in range(step["reps"]):
                round_no += 1
                if rep:
                    observer.snapshot("%s:rep%d" % (step["id"], rep))
                if scen in ALL_SERVICES:
                    _run_all_at_once(scen, step, rep, round_no, active, rec, sink, fixtures, o, common_tip,
                                     observer, digests, unstable)
                else:
                    _run_round(scen, step, rep, round_no, active, rec, sink, fixtures, o, common_tip, meta,
                               digests, unstable, opts.pause_s)
                rec.clear()  # every call is on disk; keep memory flat
            cc = compare(digests)
            cc["unstable_between_reps"] = unstable
            if unstable:
                cc["ok"] = False
            crosschecks[step["id"]] = cc
            if not cc["ok"]:
                log("  CORRECTNESS: %s" % {kk: v["mismatches"] for kk, v in cc["kinds"].items() if v["mismatches"]})
            observer.snapshot("%s:end" % step["id"])
            _write_json(run_dir / "raw" / "crosscheck.json", crosschecks)
    finally:
        observer.close()
        sink.close()
        meta["ended_utc"] = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        _write_json(run_dir / "run.json", meta)
        _write_json(run_dir / "raw" / "crosscheck.json", crosschecks)
    from .analysis import build
    build(run_dir)
    log("Wrote %s (report.md, summary.json, plots/, raw/)" % run_dir)
    return run_dir


def _run_all_at_once(scen, step, rep, round_no, active, rec, sink, fixtures, o, common_tip, observer, digests,
                     unstable):
    ctxs = [Ctx(s, rec, fixtures, o, scen, common_tip, all_services=active, observer=observer, step=step["id"],
                rep=rep, round_=round_no) for s in active]
    t0 = rec.now()
    err = None
    try:
        fn = {"tip-follow": tip_follow, "soak": soak}.get(scen)
        res = fn(ctxs, observer) if fn else mempool(ctxs)
    except Exception as exc:
        res, err = {}, "%s: %s" % (type(exc).__name__, str(exc)[:200])
        log("  failed: %s" % err)
    for c in ctxs:
        sink.write("runs", {"step": step["id"], "scenario": scen, "service": c.service.name, "rep": rep,
                            "round": round_no, "order_pos": None, "started_s": round(t0, 3),
                            "ended_s": round(rec.now(), 3), "common_tip": common_tip,
                            "result": res.get(c.service.name), "error": err, "params": step["overrides"]})
        merge_digests(digests.setdefault(c.service.name, {}), c.digests, unstable, c.service.name, rep)


def _run_round(scen, step, rep, round_no, active, rec, sink, fixtures, o, common_tip, meta, digests, unstable,
               pause_s):
    shift = rep % len(active)
    order = active[shift:] + active[:shift]  # rotate so each service takes each position
    for pos, s in enumerate(order):
        if s.name in meta["aborted"]:
            continue
        if scen == "scale":
            cap = step.get("caps", {}).get(s.name, step.get("default_cap", 8))
            if o.scale_wallets > cap:
                if rep == 0:
                    log("  %s: skipped on %s (cap %d wallets; raise with --cap %s=N)" % (step["id"], s.name, cap,
                                                                                       s.name))
                continue
        ctx = Ctx(s, rec, fixtures, o, scen, common_tip, all_services=active, step=step["id"], rep=rep,
                  round_=round_no)
        t0 = rec.now()
        res = err = tb = None
        try:
            res = PER_SERVICE[scen](ctx)
        except RateLimited as exc:
            meta["aborted"][s.name] = "rate limited in %s: %s" % (step["id"], exc)
            err = "RATE_LIMITED"
            log("  RATE LIMITED; no more calls to %s" % s.name)
        except Exception as exc:
            err = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            tb = scrub(traceback.format_exc()[-1500:])
            log("  %s rep %d on %s failed: %s" % (step["id"], rep + 1, s.name, err))
        sink.write("runs", {"step": step["id"], "scenario": scen, "service": s.name, "rep": rep, "round": round_no,
                            "order_pos": pos, "started_s": round(t0, 3), "ended_s": round(rec.now(), 3),
                            "common_tip": common_tip, "result": res, "error": err, "traceback": tb,
                            "params": step["overrides"]})
        merge_digests(digests.setdefault(s.name, {}), ctx.digests, unstable, s.name, rep)
        if res is not None:
            log("  %s rep %d %s: %s" % (step["id"], rep + 1, s.name, _one_line(res)))
        if pause_s:
            time.sleep(pause_s)


def _one_line(r: dict) -> str:
    keys = ["ok", "blocks", "wall_s", "time_to_first_block_s", "mbytes_per_s", "tx_per_s", "txids_found",
            "wallet_wall_p50_s", "all_ok", "aggregate_mbytes_per_s"]
    return ", ".join("%s=%s" % (k, r[k]) for k in keys if k in r)


def _write_json(path: pathlib.Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(scrub(json.dumps(obj, indent=1, default=str)))
