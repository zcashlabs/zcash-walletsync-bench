"""Plans: which steps a run has, and what load they put on each service.

`build_steps` turns command-line options into steps. `suite_steps` is the
recommended full plan. `estimate` runs the real scenario code against a
client that sends nothing (`DryClient`), so the request counts it prints are
the counts a real run sends. Bytes and time are estimates (see
fixtures/density.json and the --assume-* options).
"""

from __future__ import annotations

import json
import pathlib
import types
from collections import Counter

from .client import STREAMING, Client, Recorder
from .proto import service_pb2 as svc
from .scenarios import ALL_SERVICES, PER_SERVICE, Ctx, parse_span
from .synthetic import make_block
from .wallet import PROFILES

DEFAULT_REPS = {"app-open": 10, "first-sync": 5, "catch-up": 10, "enhance": 10, "transparent": 5, "mixed": 10,
                "scale": 3, "tip-follow": 1, "mempool": 1, "soak": 1}
HARD_WALLET_CAP = 8
RAISED_WALLET_CAP = 16
DENSITY_PATH = pathlib.Path(__file__).resolve().parent / "data" / "density.json"

# Response sizes seen in the 2026-11-13 validation runs, for the estimate only.
EST_BYTES = {"GetLatestBlock": 45, "GetLightdInfo": 330, "GetTreeState": 2550, "GetTransaction": 30800,
             "GetBlock": 300, "SUBTREE_ROOT": 80, "TADDR_TX": 1100, "GetAddressUtxosStream": 0,
             "GetMempoolStream": 3000}
# Transaction counts for the fixture addresses, from the same runs.
EST_TADDR_TXS = {"history_per_address": 17, "full_history": [67, 8312]}


def wallet_caps(opts) -> tuple[int, dict]:
    hard = RAISED_WALLET_CAP if getattr(opts, "allow_16_wallets", False) else HARD_WALLET_CAP
    default = min(opts.max_wallets, hard)
    caps = {}
    for item in getattr(opts, "cap", None) or []:
        name, _, n = item.partition("=")
        caps[name] = min(int(n), hard)
    return default, caps


def _step(sid, scen, reps, **ov):
    return {"id": sid, "scenario": scen, "reps": reps, "overrides": ov}


def build_steps(opts, scenarios: list[str]) -> list[dict]:
    profiles = [p for p in opts.profile.split(",") if p]
    for p in profiles:
        if p not in PROFILES:
            raise SystemExit("unknown profile %r" % p)
    reps = lambda scen: opts.reps or (opts.first_sync_reps if scen == "first-sync" else DEFAULT_REPS[scen])  # noqa
    default_cap, caps = wallet_caps(opts)
    steps = []
    for scen in scenarios:
        if scen == "first-sync":
            for p in profiles:
                for n in opts.sizes:
                    steps.append(_step("first-sync/%s/%d" % (p, n), scen, reps(scen), profile=p, blocks=n))
        elif scen == "catch-up":
            for p in profiles:
                for b in opts.behind:
                    steps.append(_step("catch-up/%s/%s" % (p, b), scen, reps(scen), profile=p, behind_one=b))
        elif scen == "scale":
            top = max([default_cap] + list(caps.values()))
            for p in profiles:
                for w in opts.scale_levels:
                    if w > top:
                        continue
                    s = _step("scale/%s/w%d" % (p, w), scen, reps(scen), profile=p, scale_wallets=w)
                    s["caps"], s["default_cap"] = caps, default_cap
                    steps.append(s)
        elif scen in ("enhance", "transparent", "mixed"):
            for p in profiles:
                steps.append(_step("%s/%s" % (scen, p), scen, reps(scen), profile=p))
        else:
            steps.append(_step(scen, scen, reps(scen), profile=profiles[0]))
    return steps


SUITE_NOTE = ("Recommended full plan: app-open x10; catch-up 1h/1d/1w x10; first-sync 1k and 10k x5 and 50k x3 "
              "(Vizor profile); first-sync 10k x5 with the Zingo and librustzcash profiles; enhance x10 (Vizor, "
              "Zingo); transparent x5; mixed x10; scale 1/2/4/8 wallets x3; tip-follow 10 min; soak 30 min.")


def suite_steps(opts) -> list[dict]:
    """The recommended plan. --reps overrides every repetition count and
    --sizes limits the first-sync sizes (the 10k profile steps need 10000)."""
    default_cap, caps = wallet_caps(opts)
    R = lambda d: opts.reps or d  # noqa: E731
    sizes = list(opts.sizes)
    s = [_step("app-open", "app-open", R(10), profile="vizor")]
    s += [_step("catch-up/vizor/%s" % b, "catch-up", R(10), profile="vizor", behind_one=b) for b in ("1h", "1d", "1w")]
    s += [_step("first-sync/vizor/%d" % n, "first-sync", R(3 if n >= 50000 else 5), profile="vizor", blocks=n)
          for n in sizes]
    if 10000 in sizes:
        s += [_step("first-sync/%s/10000" % p, "first-sync", R(5), profile=p, blocks=10000)
              for p in ("zingo", "librustzcash")]
    s += [_step("enhance/%s" % p, "enhance", R(10), profile=p) for p in ("vizor", "zingo")]
    s += [_step("transparent/vizor", "transparent", R(5), profile="vizor")]
    s += [_step("mixed/vizor", "mixed", R(10), profile="vizor")]
    for w in (1, 2, 4, 8) + ((16,) if getattr(opts, "allow_16_wallets", False) else ()):
        st = _step("scale/vizor/w%d" % w, "scale", R(3), profile="vizor", scale_wallets=w)
        st["caps"], st["default_cap"] = caps, default_cap
        if w <= max([default_cap] + list(caps.values())):
            s.append(st)
    s.append(_step("tip-follow", "tip-follow", 1, profile="vizor", duration_s=opts.tip_follow_s))
    if opts.soak_min > 0:
        s.append(_step("soak", "soak", 1, profile="vizor", soak_min=opts.soak_min))
    return s


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------

class Density:
    def __init__(self, path=DENSITY_PATH):
        d = json.loads(pathlib.Path(path).read_text())
        self.pe, self.pb = d["per_element_bytes"], d["per_block_bytes"]
        self.coarse = [w for w in d["windows"] if w[1] - w[0] > 100]
        self.fine = [w for w in d["windows"] if w[1] - w[0] <= 100]
        self.after = d["after_last_window"]

    def elements(self, h: int) -> float:
        for a, b, e in self.fine:
            if a <= h < b:
                return e
        for a, b, e in self.coarse:
            if a <= h < b:
                return e
        return self.after

    def range_bytes(self, lo: int, hi: int) -> float:
        if hi < lo:
            return 0.0
        step = 100 if hi - lo > 2000 else 1
        total, h = 0.0, lo
        while h <= hi:
            n = min(step, hi - h + 1)
            total += n * (self.pe * self.elements(h) + self.pb)
            h += n
        return total


class DryClient(Client):
    """Client that sends nothing. It records each request with an estimated
    response size, and returns synthetic responses so the scenario code runs
    its normal path."""

    def __init__(self, service, recorder, log, density, scenario="", tag=None, **kw):
        self.service, self.endpoint, self.recorder = service, "dry", recorder
        self.scenario, self.tag = scenario, tag
        self.step = self.rep = self.round = None
        self.conn_id = recorder.new_conn_id()
        self._first, self.closed = True, False
        import threading
        self._first_lock, self._tls = threading.Lock(), threading.local()
        self.connect_ms, self.connect_error = 0.0, None
        self.log, self.density = log, density
        log.append(("CONNECT", tag, 0))

    def close(self):
        self.closed = True

    def unary(self, method, request, timeout=30.0, tag=None):
        cold = self._take_cold()
        est = EST_BYTES.get(method, 100)
        if method == "GetLatestBlock":
            raw = svc.BlockID(height=self.recorder.dry_tip).SerializeToString()
        elif method == "GetBlock":
            raw = make_block(request.height).SerializeToString()
        elif method == "GetTreeState":
            raw = svc.TreeState(height=request.height).SerializeToString()
        elif method == "GetTransaction":
            raw = svc.RawTransaction(data=b"x", height=0).SerializeToString()
        else:
            raw = b""
        self.log.append((method, _describe(method, request), est))
        self._record(method, True, self.recorder.now(), 0.0, 0.0, 1, len(raw), cold, None, tag, timeout)
        return raw

    def stream(self, method, request, timeout=120.0, tag=None, limit_s=None, expect=None):
        cold = self._take_cold()
        out, est = [], 0
        if method == "GetBlockRange":
            lo, hi = request.start.height, request.end.height
            out = [make_block(h).SerializeToString() for h in range(lo, hi + 1)]
            est = self.density.range_bytes(lo, hi)
        elif method == "GetSubtreeRoots":
            n = {0: 1129, 1: 770, 2: 18}.get(request.shieldedProtocol, 0)
            n = max(0, n - request.startIndex)
            out = [svc.SubtreeRoot(completingBlockHeight=1).SerializeToString()] * n
            est = n * EST_BYTES["SUBTREE_ROOT"]
        elif method == "GetTaddressTxids":
            full = request.range.start.height <= 419200
            n = EST_TADDR_TXS["full_history"][1] if full else EST_TADDR_TXS["history_per_address"]
            est = n * EST_BYTES["TADDR_TX"]
        else:
            est = EST_BYTES.get(method, 0)
        self.log.append((method, _describe(method, request), est))
        arrivals = [(0.0, len(r)) for r in out[:1]]
        self._record(method, True, self.recorder.now(), 0.0, 0.0 if out else None, len(out),
                     sum(len(r) for r in out), cold, None, tag, timeout, arrivals or None)
        return out, True


def _describe(method, req) -> str:
    if method == "GetBlockRange":
        return "%d-%d (%d blocks)" % (req.start.height, req.end.height, req.end.height - req.start.height + 1)
    if method == "GetTreeState" or method == "GetBlock":
        return str(req.height)
    if method == "GetSubtreeRoots":
        return "protocol %d from %d" % (req.shieldedProtocol, req.startIndex)
    if method == "GetTaddressTxids":
        return "%s.. %d-%d" % (req.address[:6], req.range.start.height, req.range.end.height)
    if method == "GetAddressUtxosStream":
        return "%d addresses from %d" % (len(req.addresses), req.startHeight)
    return ""


class DryCtx(Ctx):
    def prep_client(self):
        return DryClient(self.service.name, Recorder(), [], self._density)

    def probe(self, samples):
        self._log.append(("PROBE(DNS+TCP+TLS)", "x%d" % samples, 0))
        return {"tcp_connect_ms_p50": None, "tls_handshake_ms_p50": None}

    def client(self, tag=None):
        return DryClient(self.service.name, self.recorder, self._log, self._density, scenario=self.scenario, tag=tag)


def estimate(opts, steps, fixtures, services, assume_tip: int) -> dict:
    """Run one repetition of each per-service step against DryClient; scale by
    the number of repetitions. Time-based steps are estimated from their
    timers."""
    density = Density()
    out = []
    for step in steps:
        d = dict(vars(opts))
        d.update(step["overrides"])
        d.update(pause_s=0, stagger_s=0)
        o = types.SimpleNamespace(**d)
        scen = step["scenario"]
        if scen in ALL_SERVICES:
            out.append(_timed_estimate(step, o, fixtures))
            continue
        rec = Recorder()
        rec.dry_tip = assume_tip
        log: list = []
        ctx = DryCtx(services[0], rec, fixtures, o, scen, assume_tip)
        ctx._log, ctx._density = log, density
        PER_SERVICE[scen](ctx)
        calls = [x for x in log if x[0] not in ("CONNECT", "PROBE(DNS+TCP+TLS)")]
        conns = sum(1 for x in log if x[0] == "CONNECT")
        nbytes = sum(x[2] for x in calls)
        counts = Counter(x[0] for x in calls)
        prof = PROFILES[o.profile]
        conc = max(1, min(prof.inflight, o.max_concurrency)) if scen in ("first-sync", "catch-up") else 1
        if scen == "scale":
            conc = o.scale_wallets
        secs = (len(calls) + conns) * opts.assume_rtt_ms / 1000.0 / conc + nbytes / (opts.assume_mbps * 1e6)
        if scen == "transparent":
            secs += 0  # full-history time is in the byte estimate
        out.append({"step": step["id"], "reps": step["reps"], "requests_per_rep": len(calls), "connections_per_rep": conns,
                    "bytes_per_rep": nbytes, "seconds_per_rep": secs, "methods_per_rep": dict(counts),
                    "requests": len(calls) * step["reps"], "bytes": nbytes * step["reps"],
                    "seconds": secs * step["reps"], "sequence": log, "all_at_once": False})
    return {"steps": out, "assume": {"tip": assume_tip, "rtt_ms": opts.assume_rtt_ms, "mbps": opts.assume_mbps}}


def _timed_estimate(step, o, fixtures) -> dict:
    scen = step["scenario"]
    if scen == "soak":
        secs = o.soak_min * 60
        per_min = (60 / o.poll_s) + (60 / o.soak_app_open_s) * 3 + (60 / o.soak_catch_up_s) * 8 + \
                  (60 / o.soak_enhance_s) * 5 + 60 / o.observe_s + 0.8
        nbytes = secs / 60 * ((60 / o.soak_catch_up_s) * 48 * 900 + (60 / o.soak_enhance_s) * 5 * EST_BYTES[
            "GetTransaction"] + (60 / o.soak_app_open_s) * 900)
        reqs = per_min * secs / 60
    elif scen == "tip-follow":
        secs = o.duration_s
        reqs = secs / o.poll_s + secs / o.observe_s + secs / 75 * 4
        nbytes = secs / 75 * 900
    else:  # mempool
        secs = o.duration_s
        reqs = secs / 75 + 1
        nbytes = reqs * EST_BYTES["GetMempoolStream"]
    return {"step": step["id"], "reps": step["reps"], "requests_per_rep": round(reqs), "bytes_per_rep": nbytes,
            "seconds_per_rep": secs, "requests": round(reqs) * step["reps"], "bytes": nbytes * step["reps"],
            "seconds": secs * step["reps"], "methods_per_rep": {}, "sequence": [], "all_at_once": True}


def print_plan(est: dict, services: list[str], verbose: bool = False) -> str:
    lines = []
    a = est["assume"]
    lines.append("Plan for services: %s" % ", ".join(services))
    lines.append("Estimates assume: tip %d, RTT %d ms, %.1f MB/s per stream. Request counts are exact; bytes "
                 "and time are estimates." % (a["tip"], a["rtt_ms"], a["mbps"]))
    lines.append("")
    lines.append("%-30s %5s %10s %10s %10s %12s %12s" % ("step", "reps", "req/rep", "MB/rep", "req total",
                                                         "MB/service", "time/service"))
    treq = tbytes = tsec = 0.0
    wall = 0.0
    for s in est["steps"]:
        lines.append("%-30s %5d %10d %10.2f %10d %12.1f %12s" % (
            s["step"], s["reps"], s["requests_per_rep"], s["bytes_per_rep"] / 1e6, s["requests"], s["bytes"] / 1e6,
            _dur(s["seconds"])))
        treq += s["requests"]
        tbytes += s["bytes"]
        tsec += s["seconds"]
        wall += s["seconds"] if s["all_at_once"] else s["seconds"] * len(services)
        if verbose and s["sequence"]:
            for m, arg, b in s["sequence"]:
                lines.append("      %-24s %-40s %10.1f kB" % (m, arg or "", b / 1e3))
        elif s["methods_per_rep"]:
            lines.append("      per rep: " + ", ".join("%s x%d" % kv for kv in sorted(s["methods_per_rep"].items())))
    lines.append("")
    lines.append("Per service: about %d requests, %.0f MB, %s of its own traffic." % (treq, tbytes / 1e6, _dur(tsec)))
    lines.append("Whole run (services one at a time, except tip-follow, mempool and soak): about %s." % _dur(wall))
    return "\n".join(lines)


def _dur(s: float) -> str:
    s = int(round(s))
    return "%dh%02dm" % (s // 3600, s % 3600 // 60) if s >= 3600 else "%dm%02ds" % (s // 60, s % 60)


def parse_levels(text: str) -> list[int]:
    return sorted({int(x) for x in str(text).split(",") if x})


def parse_sizes(text: str) -> list[int]:
    out = []
    for x in str(text).split(","):
        x = x.strip().lower()
        if x:
            out.append(int(float(x[:-1]) * 1000) if x.endswith("k") else int(x))
    return out


__all__ = ["build_steps", "suite_steps", "estimate", "print_plan", "parse_levels", "parse_sizes", "parse_span",
           "STREAMING"]


# --------------------------------------------------------------------------
# Stress
# --------------------------------------------------------------------------

def stress_steps(opts, services) -> list[dict]:
    from .stress import check_levels
    out = []
    for s in services:
        levels = check_levels(s, parse_levels(opts.levels))
        out.append({"id": "stress/%s" % s.name, "scenario": "stress", "reps": 1,
                    "overrides": {"service": s.name, "levels": levels, "profile": opts.profile.split(",")[0]}})
    return out


def _session_costs(opts, fixtures, assume_tip) -> dict:
    """Requests and estimated bytes of one session of each mix item, from a
    dry run of the real session code."""
    from .stress import BLOCKS_PER_HOUR
    from .wallet import sync_range
    density = Density()
    prof = PROFILES[opts.profile.split(",")[0]]
    out = {}
    fs_tip = opts.first_sync_tip or fixtures["first_sync"]["tip"]
    plans = {
        "catch-up-1h": (assume_tip - BLOCKS_PER_HOUR + 1, assume_tip, False),
        "catch-up-1d": (assume_tip - 24 * BLOCKS_PER_HOUR + 1, assume_tip, False),
        "first-sync": (fs_tip - 10000 - opts.stress_first_sync_blocks + 1, fs_tip - 10000, True),
    }
    for kind, (lo, hi, fresh) in plans.items():
        rec = Recorder()
        rec.dry_tip = assume_tip
        log: list = []
        c = DryClient("dry", rec, log, density, tag="w")
        sync_range(c, prof, lo, hi, fresh=fresh, active_taddrs=fixtures["taddrs_active"],
                   max_inflight=opts.max_concurrency, digests=None)
        calls = [x for x in log if x[0] != "CONNECT"]
        out[kind] = {"requests": len(calls), "bytes": sum(x[2] for x in calls),
                     "conc": max(1, min(prof.inflight, opts.max_concurrency))}
    out["enhance"] = {"requests": 5, "bytes": 5 * EST_BYTES["GetTransaction"], "conc": 1}
    out["poll"] = {"requests": 1, "bytes": EST_BYTES["GetLatestBlock"], "conc": 1}
    return out


def estimate_stress(opts, steps, fixtures, assume_tip) -> dict:
    from .stress import parse_mix
    costs = _session_costs(opts, fixtures, assume_tip)
    mix = parse_mix(opts.stress_mix)
    wsum = sum(w for _, w in mix)
    mean_req = sum(costs[k]["requests"] * w for k, w in mix) / wsum
    mean_bytes = sum(costs[k]["bytes"] * w for k, w in mix) / wsum
    mean_t = sum((costs[k]["requests"] * opts.assume_rtt_ms / 1000 / costs[k]["conc"]
                  + costs[k]["bytes"] / (opts.assume_mbps * 1e6)) * w for k, w in mix) / wsum
    per_wallet_sessions = opts.level_s / (mean_t + opts.stress_think_s)
    rows = []
    for st in steps:
        for level in st["overrides"]["levels"]:
            sessions = level * per_wallet_sessions
            if opts.stress_sessions:
                sessions = min(sessions, opts.stress_sessions)
            nbytes = sessions * mean_bytes
            link_cap = opts.assume_link_mbps * 1e6 * opts.level_s
            rows.append({"step": st["id"], "level": level, "sessions": sessions, "requests": sessions * mean_req,
                         "bytes": min(nbytes, link_cap), "bytes_uncapped": nbytes, "link_limited": nbytes > link_cap,
                         "seconds_max": opts.level_s + opts.stress_grace_s + opts.stress_cooldown_s + 3})
    return {"rows": rows, "costs": costs, "mean": {"requests": mean_req, "bytes": mean_bytes, "seconds": mean_t},
            "assume": {"tip": assume_tip, "rtt_ms": opts.assume_rtt_ms, "mbps": opts.assume_mbps,
                       "link_mbps": opts.assume_link_mbps}}


def print_stress_plan(est: dict, opts) -> str:
    a = est["assume"]
    m = est["mean"]
    L = ["Stress plan. Mix: %s. Think time: 0-%.1f s (mean %.1f s). Levels held %d s each "
         "(grace up to %d s, cooldown %d s)." % (opts.stress_mix, 2 * opts.stress_think_s, opts.stress_think_s,
                                                 opts.level_s, opts.stress_grace_s, opts.stress_cooldown_s),
         "Estimates assume RTT %d ms, %.1f MB/s per stream, a %.0f MB/s client link, and that the server keeps up "
         "(an upper bound). Mean session: %.1f requests, %.2f MB, %.2f s."
         % (a["rtt_ms"], a["mbps"], a["link_mbps"], m["requests"], m["bytes"] / 1e6, m["seconds"]),
         "Session costs (dry run): " + ", ".join("%s %d req %.2f MB" % (k, v["requests"], v["bytes"] / 1e6)
                                                 for k, v in est["costs"].items()),
         "",
         "%-24s %7s %10s %12s %10s %10s" % ("step", "wallets", "sessions", "requests", "MB", "max time")]
    tot_req = tot_b = tot_s = 0.0
    for r in est["rows"]:
        L.append("%-24s %7d %10.0f %12.0f %10.0f%s %9s" % (r["step"], r["level"], r["sessions"], r["requests"],
                                                         r["bytes"] / 1e6, "*" if r["link_limited"] else " ",
                                                         _dur(r["seconds_max"])))
        tot_req += r["requests"]
        tot_b += r["bytes"]
        tot_s += r["seconds_max"]
    L.append("")
    L.append("Total: about %.0f requests, %.1f GB, at most %s (less if a threshold stops the ramp). "
             "* = limited by the assumed client link." % (tot_req, tot_b / 1e9, _dur(tot_s)))
    return "\n".join(L)
