"""Geo sweep: latency to fastz, zec.rocks and zcashlabs from many cities through Proton VPN, then wallet
syncs in the cities where a competitor answers faster than fastz.

Through a VPN every number includes the trip from this machine to the VPN server. That part is the
same for every host measured from one city, so only the comparison between hosts in the same city
means something. Each round also times a DNS query to Proton's resolver inside the tunnel (the
tunnel baseline); host latency minus that baseline approximates what someone in the city would see.

Phase 1, per city: connect to a physical Proton server in the city (smart-routed locations, whose
exit is in another country, are left out), check the exit with Cloudflare's trace, resolve every
target through Proton's resolver (the system resolver may sit outside the tunnel, for example
Tailscale MagicDNS), then run interleaved rounds of one warm GetLatestBlock per target, in a new
random order each round. The best zone of a group (zec.rocks zones, fastz regions) is chosen on the
even rounds and compared on the odd rounds, so picking the minimum of several noisy zones does not
flatter it.

Phase 2, in the cities where a competitor beat fastz (and a few control cities where fastz won
narrowly): `zcash-walletsync-bench run catch-up first-sync` with every name pinned to the IPv4 address Proton's
resolver returned. Each city's run is a normal run directory under <sweep>/runs/.

Files: <sweep>/sweep.json (options, environment), <sweep>/cities.jsonl (phase 1, one line per
city), <sweep>/syncs.jsonl (phase 2, one line per city), <sweep>/logs/ (phase 2 runner output).
A sweep can be stopped and started again with --sweep-dir; finished cities are skipped.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import pathlib
import random
import re
import socket
import ssl
import statistics
import struct
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .services import FASTZ, FASTZ_REGIONS, ZCASHLABS, ZECROCKS_ZONES

SCHEMA = "zcash-walletsync-bench/geo-1"
SERVERLIST = "~/.cache/Proton/VPN/serverlist.json"
DEFAULT_VPN_DNS = "10.2.0.1"  # Proton's resolver and gateway on WireGuard connections

# Proton feature bits (logical server "Features").
F_SECURE_CORE, F_TOR = 1, 2


def log(msg: str) -> None:
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


# ------------------------------------------------------------------ targets

@dataclass(frozen=True)
class Target:
    name: str      # e.g. "zecrocks-eu"
    group: str     # fastz | fastz-region | zecrocks | zcashlabs
    endpoint: str  # host:port

    @property
    def host(self) -> str:
        return self.endpoint.rsplit(":", 1)[0].replace("plain://", "")

    @property
    def port(self) -> int:
        return int(self.endpoint.rsplit(":", 1)[1])


def default_targets() -> list[Target]:
    t = [Target("fastz", "fastz", FASTZ)]
    t += [Target("fastz-" + r, "fastz-region", e) for r, e in FASTZ_REGIONS.items()]
    t += [Target("zecrocks-" + z, "zecrocks", e) for z, e in ZECROCKS_ZONES.items()]
    t += [Target("zcashlabs", "zcashlabs", ZCASHLABS)]
    return t


COMPETITOR_GROUPS = ("zecrocks", "zcashlabs")


# ------------------------------------------------------------------ cities

@dataclass
class City:
    key: str
    country: str
    city: str
    lat: float
    lon: float
    servers: list = field(default_factory=list)  # [(name, load, score)], best first

    def label(self) -> str:
        return "%s, %s" % (self.city, self.country)


def slug(country: str, city: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", city).encode("ascii", "ignore").decode()
    return "%s-%s" % (country.lower(), re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-"))


def load_cities(path: str | os.PathLike = SERVERLIST, max_tier: int | None = None) -> list[City]:
    """Cities with at least one physical exit, from Proton's cached server list.

    Left out: smart-routed servers (HostCountry set: the exit is in another country), Secure Core
    (traffic enters in a second country), Tor servers, servers above the account's tier, and
    servers that are down.
    """
    path = pathlib.Path(os.path.expanduser(str(path)))
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as fh:
        d = json.load(fh)
    tier = d.get("MaxTier", 2) if max_tier is None else max_tier
    by: dict[tuple, City] = {}
    for x in d["LogicalServers"]:
        if x.get("HostCountry") or x["Features"] & (F_SECURE_CORE | F_TOR) or x["Tier"] > tier:
            continue
        if not x.get("Status") or not any(s.get("Status") for s in x.get("Servers", [])):
            continue
        if x["EntryCountry"] != x["ExitCountry"]:
            continue
        k = (x["ExitCountry"], x["City"])
        c = by.get(k)
        if c is None:
            loc = x.get("ExitLocation") or {}
            c = by[k] = City(slug(*k), k[0], k[1], loc.get("Latitude", 0.0), loc.get("Longitude", 0.0))
        c.servers.append((x["Name"], x.get("Load", 0), x.get("Score", 0.0)))
    for c in by.values():
        c.servers.sort(key=lambda s: (s[2], s[1]))  # Proton's score is a cost: lower is better
    return sorted(by.values(), key=lambda c: (c.country, c.city))


def km(a: City, b: City) -> float:
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dl = math.radians(b.lon - a.lon)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def thin(cities: list[City], min_km: float) -> list[City]:
    """Drop cities closer than min_km to one already kept; cities with more servers are kept first."""
    if min_km <= 0:
        return list(cities)
    kept: list[City] = []
    for c in sorted(cities, key=lambda c: (-len(c.servers), c.key)):
        if all(km(c, k) >= min_km for k in kept):
            kept.append(c)
    order = {c.key: i for i, c in enumerate(cities)}
    return sorted(kept, key=lambda c: order[c.key])


def select(cities: list[City], only: str | None, exclude: str | None) -> list[City]:
    def match(c: City, spec: str) -> bool:
        toks = [t.strip().lower() for t in spec.split(",") if t.strip()]
        return any(t in (c.country.lower(), c.city.lower(), c.key) for t in toks)
    out = [c for c in cities if not only or match(c, only)]
    return [c for c in out if not exclude or not match(c, exclude)]


# ------------------------------------------------------------------ DNS (stdlib, to query Proton's resolver)

QTYPES = {"A": 1, "NS": 2, "TXT": 16}


def _qname(name: str) -> bytes:
    labels = [x for x in name.rstrip(".").split(".") if x]
    return b"".join(bytes([len(x)]) + x.encode() for x in labels) + b"\0"


def _skip_name(buf: bytes, off: int) -> int:
    while True:
        n = buf[off]
        if n == 0:
            return off + 1
        if n & 0xC0 == 0xC0:
            return off + 2
        off += n + 1


def build_query(name: str, qtype: str, qid: int) -> bytes:
    return struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + _qname(name) + struct.pack(">HH", QTYPES[qtype], 1)


def parse_response(buf: bytes, qid: int, qtype: str) -> list[str]:
    rid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", buf[:12])
    if rid != qid:
        raise ValueError("DNS reply id mismatch")
    rcode = flags & 0xF
    if rcode not in (0, 3):  # 3 = NXDOMAIN: empty answer
        raise ValueError("DNS rcode %d" % rcode)
    off = 12
    for _ in range(qd):
        off = _skip_name(buf, off) + 4
    out = []
    for _ in range(an):
        off = _skip_name(buf, off)
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", buf[off:off + 10])
        off += 10
        rdata = buf[off:off + rdlen]
        off += rdlen
        if rtype != QTYPES[qtype]:
            continue  # CNAMEs on the way
        if rtype == 1:
            out.append(socket.inet_ntoa(rdata))
        elif rtype == 16:
            parts, i = [], 0
            while i < len(rdata):
                n = rdata[i]
                parts.append(rdata[i + 1:i + 1 + n].decode("utf-8", "replace"))
                i += 1 + n
            out.append("".join(parts))
        else:
            out.append(rdata.hex())
    return out


def resolve_retry(dns, server: str, name: str, qtype: str = "A", tries: int = 3) -> list[str]:
    """dns() with retries: UDP queries through a lossy tunnel time out now and then."""
    err = None
    for _ in range(tries):
        try:
            ans, _ms = dns(server, name, qtype)
            return ans
        except Exception as exc:  # timeout, garbled reply
            err = exc
    raise err


def dns_query(server: str, name: str, qtype: str = "A", timeout: float = 3.0) -> tuple[list[str], float]:
    """One UDP query to `server`. Returns (answers, round trip ms)."""
    qid = random.randrange(1 << 16)
    q = build_query(name, qtype, qid)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        t0 = time.perf_counter()
        s.sendto(q, (server, 53))
        while True:
            buf, _ = s.recvfrom(4096)
            if buf[:2] == q[:2]:
                break
        ms = (time.perf_counter() - t0) * 1000
    return parse_response(buf, qid, qtype), ms


# ------------------------------------------------------------------ VPN control (Proton CLI + NetworkManager)

def sh(args: list[str], timeout: float = 60.0) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout after %ss" % timeout
    except FileNotFoundError as exc:
        return 127, str(exc)


class Vpn:
    """The Proton VPN command line client. `run` is replaceable for tests."""

    def __init__(self, run=sh, cli: str = "protonvpn"):
        self.run, self.cli = run, cli

    def status(self) -> dict:
        _, out = self.run([self.cli, "status"], 30)
        st = {}
        for line in out.splitlines():
            k, sep, v = line.partition(":")
            if sep and k.strip() and not k.startswith(" "):
                st[k.strip().lower()] = v.strip()
        st["connected"] = st.get("status", "").lower().startswith("connected")
        return st

    def config(self) -> dict:
        _, out = self.run([self.cli, "config", "list"], 30)
        cfg = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and not set(parts[0]) <= {"-"}:
                cfg[parts[0]] = parts[1]
        return cfg

    def version(self) -> str | None:
        _, out = self.run([self.cli, "--help"], 30)
        m = re.search(r"\b(\d+\.\d+\.\d+)\s*$", out, re.M)
        return m.group(1) if m else None

    def connect(self, server: str, wait_s: float = 60.0) -> dict:
        rc, out = self.run([self.cli, "connect", server], 120)
        t_end = time.monotonic() + wait_s
        while True:
            st = self.status()
            if st["connected"] or time.monotonic() > t_end:
                break
            time.sleep(1.0)
        st["connect_rc"], st["connect_out"] = rc, out.strip()[-300:]
        if not st["connected"]:
            st["cleaned_up"] = self.disconnect()  # a half-made tunnel must not stay up
        return st

    def active_connections(self) -> list[str]:
        """Proton connections NetworkManager still has up. The CLI can report Disconnected while one is
        active: a connect it reported as failed can still come up (Oslo, 2026-11-17)."""
        _, out = self.run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"], 15)
        return [line.rsplit(":", 1)[0] for line in out.splitlines()
                if line.startswith("ProtonVPN") or line.lower().startswith("proton")]

    def disconnect(self) -> list[str]:
        """Disconnect, then take down any Proton connection NetworkManager still has. Returns those."""
        self.run([self.cli, "disconnect"], 60)
        left = self.active_connections()
        for name in left:
            self.run(["nmcli", "connection", "down", name], 30)
        return left

    def interface(self) -> str | None:
        _, out = self.run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"], 15)
        for line in out.splitlines():
            dev, _, rest = line.partition(":")
            typ, _, state = rest.partition(":")
            if state.startswith("connected") and (dev.startswith("proton") or typ in ("wireguard", "tun")):
                return dev
        return None

    def dns_server(self, dev: str | None) -> str:
        if dev:
            _, out = self.run(["nmcli", "-t", "-f", "IP4.DNS", "device", "show", dev], 15)
            for line in out.splitlines():
                _, _, v = line.partition(":")
                if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", v.strip()):
                    return v.strip()
        return DEFAULT_VPN_DNS

    def route_dev(self, ip: str) -> str | None:
        _, out = self.run(["ip", "route", "get", ip], 15)
        m = re.search(r"\bdev (\S+)", out)
        return m.group(1) if m else None


def _trace(url: str, timeout: float) -> dict:
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:
        return dict(line.split("=", 1) for line in r.read().decode().splitlines() if "=" in line)


def cloudflare_trace(timeout: float = 8.0) -> dict:
    """colo, loc (country), ip (IPv4) and ip6 (IPv6, when the tunnel has it), from Cloudflare's
    resolver addresses (no DNS involved). Callers must not store `ip` or `ip6`."""
    kv = _trace("https://1.1.1.1/cdn-cgi/trace", timeout)
    out = {"colo": kv.get("colo"), "loc": kv.get("loc"), "ip": kv.get("ip"), "ip6": None}
    try:
        out["ip6"] = _trace("https://[2606:4700:4700::1111]/cdn-cgi/trace", timeout / 2).get("ip")
    except Exception:
        pass
    return out


def same_net(a: str | None, b: str | None) -> bool | None:
    """Same /24 (IPv4) or /48 (IPv6); None when either is missing or the families differ."""
    import ipaddress
    if not a or not b:
        return None
    try:
        x, y = ipaddress.ip_address(a), ipaddress.ip_address(b)
    except ValueError:
        return None
    if x.version != y.version:
        return None
    bits = 24 if x.version == 4 else 48
    return ipaddress.ip_network("%s/%d" % (x, bits), strict=False) == ipaddress.ip_network(
        "%s/%d" % (y, bits), strict=False)


def cc(code: str | None) -> str | None:
    return {"UK": "GB"}.get(code or "", code)


# ------------------------------------------------------------------ measurement

def _channel(t: Target, ip: str):
    import grpc

    from .client import h2_options
    opts = h2_options() + [("grpc.enable_http_proxy", 0), ("grpc.use_local_subchannel_pool", 1)]
    if t.endpoint.startswith("plain://"):
        return grpc.insecure_channel("%s:%d" % (ip, t.port), options=opts)
    opts += [("grpc.ssl_target_name_override", t.host), ("grpc.default_authority", t.host)]
    return grpc.secure_channel("ipv4:%s:%d" % (ip, t.port), grpc.ssl_channel_credentials(), options=opts)


def tcp_connect_ms(ip: str, port: int, samples: int = 3, timeout: float = 5.0) -> list[float]:
    out = []
    for _ in range(samples):
        t0 = time.perf_counter()
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                out.append(round((time.perf_counter() - t0) * 1000, 2))
        except OSError:
            pass
    return out


@dataclass
class Env:
    """Everything phase 1 touches outside this process; replaced in the tests."""
    vpn: Vpn
    dns: object = dns_query            # (server, name, qtype) -> (answers, ms)
    trace: object = cloudflare_trace   # () -> {colo, loc, ip}
    home_country: str | None = None
    dns_server: str = DEFAULT_VPN_DNS  # used when vpn is None (tests)


def measure_city(city: City, server: str, targets: list[Target], env: Env, rounds: int = 24,
                 timeout_s: float = 10.0, tcp_samples: int = 3, pause_s: float = 0.1, seed: int = 1) -> dict:
    """Phase 1 for one city. Connects the VPN (when env.vpn is set), measures, returns the record."""
    from .proto import service_pb2 as svc
    from .proto import service_pb2_grpc as svc_grpc

    rec: dict = {"city": city.key, "country": city.country, "name": city.city, "lat": city.lat, "lon": city.lon,
                 "server": server, "started_utc": _now(), "ok": False}
    if env.vpn is not None:
        st = env.vpn.connect(server)
        rec["vpn"] = {k: st.get(k) for k in ("status", "server", "load", "protocol", "connect_rc")}
        if not st["connected"]:
            rec["error"] = "VPN did not connect: %s" % st.get("connect_out", "")[:200]
            return rec
        rec["vpn"]["server_matches"] = (server.lower() in st["server"].lower()) if st.get("server") else None
        dev = env.vpn.interface()
        vpn_dns = env.vpn.dns_server(dev)
        rec["vpn"]["interface"], rec["vpn"]["dns"] = dev, vpn_dns
        rec["vpn"]["dns_route_dev"] = env.vpn.route_dev(vpn_dns)
    else:
        vpn_dns = env.dns_server
    time.sleep(0 if env.vpn is None else 2.0)  # let routes settle

    # Where does the traffic leave? The exit IP is used for the resolver check and then dropped.
    exit_ip = exit_ip6 = None
    try:
        tr = env.trace()
        exit_ip, exit_ip6 = tr.pop("ip", None), tr.pop("ip6", None)
        rec["exit"] = {"colo": tr.get("colo"), "loc": tr.get("loc"),
                       "matches": cc(tr.get("loc")) == cc(city.country)}
    except Exception as exc:
        rec["exit"] = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:120]), "matches": None}
    if env.home_country and rec["exit"].get("loc") == env.home_country and cc(city.country) != env.home_country:
        rec["error"] = "traffic still leaves from the home country: the VPN is not routing"
        rec["fatal"] = True
        return rec
    if rec["exit"].get("matches") is False:
        rec["error"] = "exit is in %s, not %s" % (rec["exit"].get("loc"), city.country)
        return rec

    # Which resolver location does Cloudflare's load balancer see? Google's whoami name returns the
    # address that asked (and the client subnet, when the resolver forwards one).
    try:
        txt, _ = env.dns(vpn_dns, "o-o.myaddr.l.google.com", "TXT")
        egress = next((t for t in txt if re.fullmatch(r"[0-9a-f.:]+", t)), None)
        ecs = next((t.split()[-1] for t in txt if t.startswith("edns0-client-subnet")), None)
        exit_same = exit_ip6 if egress and ":" in egress else exit_ip
        rec["resolver"] = {"egress": egress, "ecs": ecs, "same_net_as_exit": same_net(egress, exit_same),
                           "family": None if not egress else 6 if ":" in egress else 4}
    except Exception as exc:
        rec["resolver"] = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
    exit_ip = exit_ip6 = None

    # Resolve every target through the tunnel's resolver.
    addrs: dict[str, str] = {}
    rec["resolved"] = {}
    for t in targets:
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", t.host):
            addrs[t.name] = t.host
            continue
        try:
            ans = resolve_retry(env.dns, vpn_dns, t.host, "A")
            rec["resolved"][t.host] = ans
            if ans:
                addrs[t.name] = ans[0]
        except Exception as exc:
            rec["resolved"][t.host] = "error: %s" % str(exc)[:80]
    fz = addrs.get("fastz")
    region_ip = {t.name[len("fastz-"):]: addrs.get(t.name) for t in targets if t.group == "fastz-region"}
    rec["fastz_dns_region"] = next((r for r, ip in region_ip.items() if ip and ip == fz), None)

    live = [t for t in targets if t.name in addrs]
    rec["tcp_connect_ms"] = {t.name: tcp_connect_ms(addrs[t.name], t.port, tcp_samples) for t in live}

    chans = {t.name: _channel(t, addrs[t.name]) for t in live}
    stubs = {n: svc_grpc.CompactTxStreamerStub(c).GetLatestBlock for n, c in chans.items()}
    dead: dict[str, str] = {}
    try:
        for t in live:  # warm-up: connection setup and the first call are not measured
            errs = []
            for _ in range(2):
                try:
                    stubs[t.name](svc.ChainSpec(), timeout=timeout_s)
                except Exception as exc:
                    errs.append(_code(exc))
                    if errs[-1] == "DEADLINE_EXCEEDED" and len(errs) == 1:
                        errs.append(errs[0])  # a timeout is not retried: the target counts as down
                        break
            if errs:
                dead[t.name] = errs[-1] if len(errs) == 2 else "1 of 2 failed: %s" % errs[0]
        rng = random.Random("%s:%d" % (city.key, seed))
        rec["rounds"] = []
        # A target whose two warm-up calls both failed is not measured in this city.
        both_failed = {n for n, e in dead.items() if not e.startswith("1 of")}
        fails = {t.name: 3 if t.name in both_failed else 0 for t in live}
        for r in range(rounds):
            row: dict = {"ms": {}, "err": {}}
            try:
                _, row["baseline_ms"] = env.dns(vpn_dns, ".", "NS")
                row["baseline_ms"] = round(row["baseline_ms"], 2)
            except Exception:
                row["baseline_ms"] = None
            order = [t for t in live if fails[t.name] < 3]
            rng.shuffle(order)
            for t in order:
                t0 = time.perf_counter()
                try:
                    stubs[t.name](svc.ChainSpec(), timeout=timeout_s)
                    row["ms"][t.name] = round((time.perf_counter() - t0) * 1000, 2)
                    fails[t.name] = 0
                except Exception as exc:
                    row["err"][t.name] = _code(exc)
                    fails[t.name] += 1
            rec["rounds"].append(row)
            if pause_s:
                time.sleep(pause_s)
    finally:
        for c in chans.values():
            c.close()
    rec["warmup_errors"] = dead
    rec["ended_utc"] = _now()
    rec["ok"] = True
    return rec


def _code(exc) -> str:
    code = getattr(exc, "code", None)
    try:
        return code().name if callable(code) else type(exc).__name__
    except Exception:
        return type(exc).__name__


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ------------------------------------------------------------------ analysis of one city

def _values(rounds: list[dict], name: str) -> list[float]:
    return [r["ms"][name] for r in rounds if name in r["ms"]]


def _best(rounds: list[dict], names: list[str], min_n: int) -> str | None:
    meds = {n: statistics.median(v) for n in names if len(v := _values(rounds, n)) >= min_n}
    return min(meds, key=meds.get) if meds else None


def _paired(rounds: list[dict], a: str, b: str, margin_ms: float) -> dict:
    """Median of (a - b) over the rounds where both answered, with a bootstrap 95% CI.
    verdict: 'faster' when a is faster than b by more than margin_ms, 'slower', or 'tie'."""
    from .stats import bootstrap_ci
    d = [r["ms"][a] - r["ms"][b] for r in rounds if a in r["ms"] and b in r["ms"]]
    if not d:
        return {"n": 0, "verdict": "no data"}
    out = {"n": len(d), "median_ms": round(statistics.median(d), 2)}
    if len(d) >= 5:
        lo, hi = bootstrap_ci(d)
        out["ci95_ms"] = [round(lo, 2), round(hi, 2)]
        out["verdict"] = "faster" if hi < -margin_ms else "slower" if lo > margin_ms else "tie"
    else:
        out["verdict"] = "too few rounds"
    return out


def summarize_city(rec: dict, targets: list[Target], margin_ms: float = 1.0) -> dict:
    """Per-target statistics and the comparison of each competitor with fastz.

    The best zone of a group is chosen on the even rounds and compared on the odd rounds.
    `qualifies` is True when some competitor is faster than fastz as DNS steers it, or than the
    best fastz region.
    """
    rounds = rec.get("rounds") or []
    if not rec.get("ok") or not rounds:
        return {"ok": False}
    sel, ev = rounds[0::2], rounds[1::2]
    min_n = max(2, len(sel) // 2)
    base = [r["baseline_ms"] for r in rounds if r.get("baseline_ms") is not None]
    base_p50 = statistics.median(base) if base else None
    per = {}
    for t in targets:
        v = _values(rounds, t.name)
        errs = sum(1 for r in rounds if t.name in r["err"])
        if not v:
            per[t.name] = {"n": 0, "errors": errs}
            continue
        v.sort()
        p50 = statistics.median(v)
        per[t.name] = {"n": len(v), "errors": errs, "min": v[0], "p50": round(p50, 2),
                       "p90": v[min(len(v) - 1, int(0.9 * len(v)))],
                       "p50_minus_tunnel": round(p50 - base_p50, 2) if base_p50 is not None else None}
    by_group: dict[str, list[str]] = {}
    for t in targets:
        by_group.setdefault(t.group, []).append(t.name)
    fastz_best = _best(sel, by_group.get("fastz-region", []), min_n)
    comps = {g: _best(sel, by_group.get(g, []), min_n) for g in COMPETITOR_GROUPS}
    vs = {}
    for g, name in comps.items():
        if not name:
            continue
        vs[g] = {"target": name, "vs_fastz": _paired(ev, name, "fastz", margin_ms)}
        if fastz_best:
            vs[g]["vs_fastz_best"] = _paired(ev, name, fastz_best, margin_ms)
    beats_dns = [g for g, v in vs.items() if v["vs_fastz"]["verdict"] == "faster"]
    beats_best = [g for g, v in vs.items() if v.get("vs_fastz_best", {}).get("verdict") == "faster"]
    steering = None
    if fastz_best:
        dns_region, best_region = rec.get("fastz_dns_region"), fastz_best[len("fastz-"):]
        steering = {"dns_region": dns_region, "best_region": best_region,
                    "dns_vs_best": _paired(ev, "fastz", fastz_best, margin_ms),
                    # The same server on two connections can differ by a few ms (the two connections
                    # can take different paths), so only a different region counts as mis-steering.
                    "misteered": False}
        if dns_region and dns_region != best_region:
            steering["misteered"] = steering["dns_vs_best"]["verdict"] == "slower"
    return {"ok": True, "rounds": len(rounds), "tunnel_p50_ms": base_p50, "targets": per,
            "fastz_best": fastz_best, "competitors": vs, "steering": steering,
            "beats_fastz_dns": beats_dns, "beats_fastz_best": beats_best,
            "qualifies": bool(beats_dns or beats_best)}


# ------------------------------------------------------------------ phase 2 selection

def lead_ms(summary: dict) -> float | None:
    """How far the strongest competitor is ahead of fastz (DNS-steered), in ms (positive: competitor
    faster). None without data."""
    meds = [-v["vs_fastz"]["median_ms"] for v in summary.get("competitors", {}).values()
            if v["vs_fastz"].get("n")]
    return max(meds) if meds else None


def pick_sync_cities(records: list[dict], targets: list[Target], n: int, controls: int,
                     margin_ms: float) -> list[tuple[dict, dict, str]]:
    """(record, summary, why) for phase 2: `n` qualifying cities spread evenly over the range of the
    competitor's lead (always the largest and the smallest), so the syncs show both a small latency
    handicap, where a faster server might make up for it, and a large one; then `controls` cities where
    fastz won with the smallest clear margin."""
    rows = []
    for rec in records:
        s = summarize_city(rec, targets, margin_ms)
        if s.get("ok"):
            rows.append((rec, s, lead_ms(s)))
    q = sorted([r for r in rows if r[1]["qualifies"]], key=lambda r: -(r[2] or 0))
    if 1 < n < len(q):
        q = [q[round(i * (len(q) - 1) / (n - 1))] for i in range(n)]
    else:
        q = q[:n]
    won = [r for r in rows if not r[1]["qualifies"] and r[2] is not None and all(
        v["vs_fastz"]["verdict"] == "slower" for v in r[1]["competitors"].values())]
    c = sorted(won, key=lambda r: -(r[2] or 0))[:controls]  # lead is negative: closest to zero first
    return [(r, s, "competitor faster") for r, s, _ in q] + [(r, s, "control") for r, s, _ in c]


def sync_services(summary: dict, why: str) -> tuple[list[str], str | None]:
    """Services for a city's sync run, and the zec.rocks zone. fastz as DNS steers it, plus the best
    region when DNS chose another; the competitors that beat fastz (for a control: the closest)."""
    svcs = ["fastz"]
    st = summary.get("steering") or {}
    if st.get("best_region") and st.get("best_region") != st.get("dns_region"):
        svcs.append("fastz-" + st["best_region"])
    comps = summary.get("competitors", {})
    groups = sorted(set(summary["beats_fastz_dns"]) | set(summary["beats_fastz_best"]))
    if why == "control" or not groups:
        groups = [min(comps, key=lambda g: comps[g]["vs_fastz"].get("median_ms", 1e9))] if comps else []
    zone = None
    for g in groups:
        svcs.append(g)
        if g == "zecrocks":
            zone = comps[g]["target"][len("zecrocks-"):]
    return svcs, zone


# ------------------------------------------------------------------ files

def read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # a line cut short by a crash
    return out


def run_has_data(run_dir: pathlib.Path) -> bool:
    """A phase-2 run directory whose summary has at least one repetition of some step."""
    p = run_dir / "summary.json"
    if not p.exists():
        return False
    try:
        steps = json.loads(p.read_text()).get("steps") or {}
    except json.JSONDecodeError:
        return False
    return any((ps.get("reps") or 0) > (ps.get("reps_failed") or 0)
               for st in steps.values() for ps in (st.get("per_service") or {}).values())


def latest_ok(records: list[dict]) -> dict[str, dict]:
    out = {}
    for r in records:
        if r.get("ok"):
            out[r["city"]] = r
    return out


def best_records(records: list[dict]) -> dict[str, dict]:
    """Per city, the ok phase-1 record to analyse: one that measured fastz, then the one that resolved
    the most targets, then the latest. A re-measure can come out worse than the first try (Berlin: the
    retry lost rpc.fastz.cash to DNS timeouts)."""
    out: dict[str, tuple] = {}
    for i, r in enumerate(records):
        if not r.get("ok"):
            continue
        has_fastz = any("fastz" in x.get("ms", {}) for x in r.get("rounds") or [])
        resolved = sum(1 for v in (r.get("resolved") or {}).values() if isinstance(v, list) and v)
        key = (has_fastz, resolved, i)
        if r["city"] not in out or key > out[r["city"]][0]:
            out[r["city"]] = (key, r)
    return {k: v[1] for k, v in out.items()}


def append_jsonl(path: pathlib.Path, obj: dict) -> None:
    with open(path, "a") as fh:
        fh.write(json.dumps(obj, default=str) + "\n")


# ------------------------------------------------------------------ plan

def plan_text(cities: list[City], targets: list[Target], a) -> str:
    T = len(targets)
    comp = [t for t in targets if t.group in COMPETITOR_GROUPS]
    per_target = 2 + a.rounds
    lines = ["Geo sweep plan (nothing is sent and the VPN is not touched)", ""]
    by_cc: dict[str, list[str]] = {}
    for c in cities:
        by_cc.setdefault(c.country, []).append(c.city)
    lines.append("Cities: %d (physical Proton exits; thinned to >= %g km apart)" % (len(cities), a.thin_km))
    for k in sorted(by_cc):
        lines.append("  %s %2d  %s" % (k, len(by_cc[k]), ", ".join(by_cc[k])))
    lines += ["", "Targets per city (%d): %s" % (T, ", ".join(t.name for t in targets)), ""]
    lines.append("Phase 1 per city: VPN connect; Cloudflare trace (1.1.1.1); %d DNS lookups and %d tunnel-baseline "
                 "queries to Proton's resolver; %d TCP connects per target; %d GetLatestBlock per target "
                 "(2 warm-up + %d rounds, one call per target per round, random order)."
                 % (T + 1, a.rounds, a.tcp_samples, per_target, a.rounds))
    lines.append("Phase 1 load on each third-party endpoint: %d tiny calls per city, %d over the sweep "
                 "(%d endpoints: %s)." % (per_target, per_target * len(cities), len(comp),
                                         ", ".join(t.name for t in comp)))
    rtt = a.assume_rtt_ms / 1000
    city_s = 15 + 5 + T * a.tcp_samples * rtt + T * per_target * (rtt + 0.02) + a.rounds * (0.1 + rtt)
    lines.append("Phase 1 time: about %.0f s per city at %d ms round trips, %.0f min in all."
                 % (city_s, a.assume_rtt_ms, city_s * len(cities) / 60))
    lines += ["", "Phase 2: up to %d cities where a competitor beat fastz, plus %d control cities." % (
        a.sync_cities, a.controls)]
    lines.append("  Each runs: zcash-walletsync-bench run catch-up first-sync --profile vizor --behind %s --sizes %s --reps %d "
                 "--ipv4 --resolve HOST=IP... --services fastz[,fastz-<best region>],<competitors>"
                 % (a.sync_behind, a.sync_sizes, a.sync_reps))
    lines.append("  Load and time per city: zcash-walletsync-bench plan catch-up first-sync --profile vizor --behind %s --sizes "
                 "%s --reps %d --services fastz,zecrocks,zcashlabs" % (a.sync_behind, a.sync_sizes, a.sync_reps))
    lines += ["", "While the sweep runs, all of this machine's traffic goes through the VPN (Tailscale funnel, "
              "this terminal's network, everything)."]
    return "\n".join(lines)


# ------------------------------------------------------------------ the sweep

def preflight(vpn: Vpn, a) -> dict:
    """Checks before any connection. Returns environment facts for sweep.json; raises SystemExit."""
    st = vpn.status()
    left = vpn.active_connections()
    if left:
        raise SystemExit("NetworkManager still has a Proton connection up (%s) although the CLI says %s. "
                         "Take it down first: nmcli connection down '%s'" % (", ".join(left), st.get("status"),
                                                                              left[0]))
    if st["connected"]:
        raise SystemExit("Proton VPN is already connected (%s). Disconnect first; the sweep manages "
                         "the connection itself." % st.get("server", "?"))
    cfg = vpn.config()
    if not cfg:
        raise SystemExit("could not read `protonvpn config list`; is the CLI installed and signed in?")
    if cfg.get("vpn-accelerator") == "on" and not a.allow_accelerator:
        raise SystemExit("VPN Accelerator is on. It can end TCP connections at the VPN server, which makes "
                         "connect times measure only the trip to Proton. Turn it off with "
                         "`protonvpn config set vpn-accelerator off`, or pass --allow-accelerator.")
    if cfg.get("kill-switch") not in (None, "off"):
        log("Note: the kill switch is %s; traffic is blocked between cities." % cfg["kill-switch"])
    return {"proton_cli": vpn.version(), "proton_config": cfg}


def run_phase1(a, cities: list[City], targets: list[Target], sweep: pathlib.Path, env: Env) -> None:
    path = sweep / "cities.jsonl"
    done = latest_ok(read_jsonl(path))
    todo = [c for c in cities if a.redo or c.key not in done]
    log("Phase 1: %d cities to measure (%d done before)" % (len(todo), len(cities) - len(todo)))
    for i, c in enumerate(todo, 1):
        if not c.servers:
            continue
        rec = None
        for server, load, _score in c.servers[:a.server_tries]:
            log("[%d/%d] %s via %s (load %s%%)" % (i, len(todo), c.label(), server, load))
            try:
                rec = measure_city(c, server, targets, env, rounds=a.rounds, timeout_s=a.call_timeout_s,
                                   tcp_samples=a.tcp_samples, seed=a.seed)
            except Exception as exc:
                rec = {"city": c.key, "server": server, "ok": False, "started_utc": _now(),
                       "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
            if rec.get("fatal"):
                append_jsonl(path, rec)
                raise SystemExit("stopping: %s" % rec["error"])
            if rec.get("ok"):
                break
            log("  failed: %s" % rec.get("error"))
            append_jsonl(path, rec)
        if rec and rec.get("ok"):
            rec["summary"] = summarize_city(rec, targets, a.margin_ms)
            append_jsonl(path, rec)
            log("  " + one_line(rec["summary"], rec))


def one_line(s: dict, rec: dict) -> str:
    if not s.get("ok"):
        return "no data"
    t = s["targets"]
    parts = ["tunnel %.0f ms" % (s["tunnel_p50_ms"] or 0)]
    parts.append("fastz(dns:%s) %s" % (rec.get("fastz_dns_region"), t.get("fastz", {}).get("p50")))
    if s.get("fastz_best"):
        parts.append("%s %s" % (s["fastz_best"], t[s["fastz_best"]].get("p50")))
    for g, v in s["competitors"].items():
        parts.append("%s %s [%s]" % (v["target"], t[v["target"]].get("p50"), v["vs_fastz"]["verdict"]))
    return ", ".join(parts) + ("  -> phase 2" if s["qualifies"] else "")


def run_phase2(a, cities: list[City], targets: list[Target], sweep: pathlib.Path, env: Env) -> None:
    recs = list(best_records(read_jsonl(sweep / "cities.jsonl")).values())
    keys = {c.key for c in cities}
    recs = [r for r in recs if r["city"] in keys]
    picks = [(r, s, w, None) for r, s, w in pick_sync_cities(recs, targets, a.sync_cities, a.controls,
                                                            a.margin_ms)]
    picks += repeat_picks(getattr(a, "repeat_syncs", None), recs, targets, a.margin_ms,
                          {p[0]["city"] for p in picks})
    path = sweep / "syncs.jsonl"
    done = {k: r for k, r in latest_ok(read_jsonl(path)).items()
            if r.get("run_dir") and run_has_data(sweep / r["run_dir"])}
    picks = [p for p in picks if a.redo or p[0]["city"] not in done]
    log("Phase 2: %d cities (%s)" % (len(picks), ", ".join("%s: %s" % (p[0]["city"], p[2]) for p in picks)))
    by_key = {c.key: c for c in cities}
    (sweep / "logs").mkdir(exist_ok=True)
    for i, (rec, summ, why, same) in enumerate(picks, 1):
        city = by_key[rec["city"]]
        svcs, zone = same or sync_services(summ, why)
        out = {"city": city.key, "why": why, "services": svcs, "zecrocks_zone": zone, "started_utc": _now(),
               "ok": False}
        servers = [rec["server"]] + [s[0] for s in city.servers if s[0] != rec["server"]][:a.server_tries - 1]
        for server in servers:
            log("[%d/%d] %s (%s) via %s: %s" % (i, len(picks), city.label(), why, server, ",".join(svcs)))
            st = env.vpn.connect(server)
            if not st["connected"]:
                out["error"] = "VPN did not connect"
                continue
            out["server"] = server
            out.pop("error", None)
            time.sleep(2.0)
            try:
                tr = env.trace()
                tr.pop("ip", None)
                tr.pop("ip6", None)
                out["exit"] = tr
            except Exception as exc:
                out["exit"] = {"error": str(exc)[:120]}
            if env.home_country and out["exit"].get("loc") == env.home_country and cc(city.country) != \
                    env.home_country:
                append_jsonl(path, {**out, "error": "VPN not routing"})
                raise SystemExit("stopping: traffic still leaves from the home country")
            break
        else:
            append_jsonl(path, out)
            continue
        dev = env.vpn.interface()
        vpn_dns = env.vpn.dns_server(dev)
        hosts = sorted({_service_host(s, zone) for s in svcs})
        pins = {}
        for h in hosts:
            try:
                ans = resolve_retry(env.dns, vpn_dns, h, "A")
                if ans:
                    pins[h] = ans[0]
            except Exception as exc:
                log("  could not resolve %s through the tunnel: %s" % (h, exc))
        out["resolve"] = pins
        if len(pins) < len(hosts):
            out["error"] = "could not resolve every host through the tunnel"
            append_jsonl(path, out)
            continue
        label = "geo-" + city.key
        argv = [sys.executable, "-m", "zcash_walletsync_bench", "run", "catch-up", "first-sync",
                "--services", ",".join(svcs), "--profile", "vizor", "--behind", a.sync_behind,
                "--sizes", a.sync_sizes, "--reps", str(a.sync_reps), "--ipv4",
                "--label", label, "--out-dir", str(sweep / "runs")]
        if zone:
            argv += ["--zecrocks-zone", zone]
        for h, ip in pins.items():
            argv += ["--resolve", "%s=%s" % (h, ip)]
        out["argv"] = argv[3:]
        before = set((sweep / "runs").glob(label + "-*")) if (sweep / "runs").exists() else set()
        logf = sweep / "logs" / ("%s.log" % city.key)
        with open(logf, "a") as fh:
            fh.write("\n$ %s\n" % " ".join(argv[3:]))
            fh.flush()
            try:
                p = subprocess.run(argv, stdout=fh, stderr=subprocess.STDOUT, timeout=a.sync_timeout_min * 60,
                                   stdin=subprocess.DEVNULL)
                out["returncode"] = p.returncode
            except subprocess.TimeoutExpired:
                out["returncode"] = "timeout"
        made = sorted(set((sweep / "runs").glob(label + "-*")) - before)
        out["run_dir"] = str(made[-1].relative_to(sweep)) if made else None
        out["ok"] = out["returncode"] == 0 and bool(made) and run_has_data(made[-1])
        if out["returncode"] == 0 and made and not out["ok"]:
            out["error"] = "the run stopped without data (see %s)" % logf.relative_to(sweep)
        out["ended_utc"] = _now()
        append_jsonl(path, out)
        log("  %s -> %s" % ("done" if out["ok"] else "FAILED (%s)" % out["returncode"], out["run_dir"]))


def repeat_picks(old_sweep, recs: list[dict], targets: list[Target], margin_ms: float,
                 taken: set) -> list[tuple[dict, dict, str, tuple]]:
    """Phase-2 cities of an earlier sweep that this sweep's own selection did not pick, with the earlier
    run's services and zec.rocks zone, so the two sweeps can be compared city by city."""
    if not old_sweep:
        return []
    by_city = {r["city"]: r for r in recs}
    out = []
    for key, sy in sorted(latest_ok(read_jsonl(pathlib.Path(old_sweep) / "syncs.jsonl")).items()):
        if key in taken or key not in by_city or not sy.get("services"):
            continue
        s = summarize_city(by_city[key], targets, margin_ms)
        if s.get("ok"):
            out.append((by_city[key], s, "repeat (%s)" % sy.get("why", "?"),
                        (list(sy["services"]), sy.get("zecrocks_zone"))))
    return out


def _service_host(name: str, zone: str | None) -> str:
    from .services import FASTZ_REGIONS as R
    if name == "fastz":
        return FASTZ.rsplit(":", 1)[0]
    if name.startswith("fastz-"):
        return R[name[len("fastz-"):]].rsplit(":", 1)[0]
    if name == "zecrocks":
        return ZECROCKS_ZONES[zone or "eu"].rsplit(":", 1)[0]
    if name == "zcashlabs":
        return ZCASHLABS.rsplit(":", 1)[0]
    raise ValueError(name)


def main(a) -> int:
    cities = select(load_cities(a.serverlist, a.max_tier), a.cities, a.exclude)
    cities = thin(cities, a.thin_km)
    if a.max_cities:
        cities = cities[:a.max_cities]
    targets = default_targets()
    if a.plan:
        print(plan_text(cities, targets, a))
        return 0
    if not cities:
        raise SystemExit("no cities selected")
    sweep = pathlib.Path(a.sweep_dir) if a.sweep_dir else pathlib.Path(a.out_dir) / ("%s-%s" % (a.label, _now()))
    vpn = Vpn()
    facts = preflight(vpn, a)
    if not a.yes:
        if not sys.stdin.isatty():
            raise SystemExit("the sweep reroutes this machine's traffic through the VPN: pass --yes, or run "
                             "it from a terminal and confirm")
        print(plan_text(cities, targets, a))
        if input("\nStart the geo sweep (phase %s) in %s? Type yes: " % (a.phase, sweep)).strip().lower() != "yes":
            print("Not started.")
            return 1
    sweep.mkdir(parents=True, exist_ok=True)
    try:
        home = cloudflare_trace().get("loc")
    except Exception:
        home = None
    meta_path = sweep / "sweep.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {"schema": SCHEMA, "sessions": []}
    meta["sessions"].append({"started_utc": _now(), "phase": a.phase, "home_country": home, **facts,
                             "options": {k: v for k, v in sorted(vars(a).items()) if not callable(v)},
                             "cities": [c.key for c in cities], "targets": [vars(t) for t in targets]})
    meta_path.write_text(json.dumps(meta, indent=1, default=str))
    env = Env(vpn=vpn, home_country=home)
    try:
        if a.phase in ("1", "both"):
            run_phase1(a, cities, targets, sweep, env)
        if a.phase in ("2", "both"):
            run_phase2(a, cities, targets, sweep, env)
    finally:
        log("Disconnecting the VPN")
        left = vpn.disconnect()
        if left:
            log("Took down Proton connections NetworkManager still had: %s" % ", ".join(left))
        if vpn.active_connections():
            log("WARNING: a Proton connection is still up; check `nmcli connection show --active`")
    from .georeport import build
    print(build(sweep))
    return 0
