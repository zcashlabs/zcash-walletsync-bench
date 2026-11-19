"""The services under test and zec.rocks zone selection."""

from __future__ import annotations

import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import grpc

from .client import open_channel
from .proto import service_pb2 as svc
from .proto import service_pb2_grpc as svc_grpc

# The default target: a server on this machine, plaintext (h2c), on the
# lightwalletd default port. `--endpoint local=...` sets another address.
LOCAL = "plain://127.0.0.1:9067"

# Presets: public mainnet services. They are third-party for every user of
# this tool, and `--own` cannot change that.
ZCASHLABS = "lwd.zcashlabsrpc.dev:443"

# The fastz cluster: one name with geo-steered DNS, and one name per region.
FASTZ = "rpc.fastz.cash:443"
FASTZ_REGIONS = {
    "usw": "usw.fastz.cash:443",  # Hillsboro, US
    "use": "use.fastz.cash:443",  # Warrenton, US
    "eu": "eu.fastz.cash:443",    # Frankfurt, DE
    "ap": "ap.fastz.cash:443",    # Singapore
    "sa": "sa.fastz.cash:443",    # São Paulo, BR
    "jp": "jp.fastz.cash:443",    # Tokyo, JP
    "lon": "lon.fastz.cash:443",  # London, UK
}
PRESETS = {"zcashlabs", "zecrocks", "fastz"} | {"fastz-" + r for r in FASTZ_REGIONS}

# Services the operator of this tool runs. Only these may get more wallets
# than the third-party cap. `local` is own; `--own NAME` adds an `--endpoint`.
OWN = {"local"}
THIRD_PARTY_MAX_WALLETS = 8

# Same zones as the original load-test gist. Zones that do not answer are skipped.
# "global" is zec.rocks itself (Fly.io anycast in front of the regional servers);
# it is never chosen by "auto", which compares the regional zones.
ZECROCKS_ZONES = {
    "global": "zec.rocks:443",
    "na": "na.zec.rocks:443",
    "sa": "sa.zec.rocks:443",
    "eu": "eu.zec.rocks:443",
    "ap": "ap.zec.rocks:443",
    "me": "me.zec.rocks:443",
}


@dataclass
class Service:
    name: str
    endpoint: str
    note: str = ""
    own: bool = False

    @property
    def kind(self) -> str:
        return "own" if self.own else "third-party"


def describe_error(exc: BaseException) -> str:
    """One line for a failed call: the gRPC status and its detail."""
    if isinstance(exc, grpc.RpcError) and hasattr(exc, "code"):
        return "%s: %s" % (exc.code().name, (exc.details() or "")[:160])
    return "%s: %s" % (type(exc).__name__, str(exc)[:160])


def lightd_info(endpoint: str, timeout: float = 15.0) -> dict:
    ch = open_channel(endpoint)
    try:
        i = svc_grpc.CompactTxStreamerStub(ch).GetLightdInfo(svc.Empty(), timeout=timeout)
        return {
            "vendor": i.vendor,
            "version": i.version,
            "chain_name": i.chainName,
            "block_height": i.blockHeight,
            "estimated_height": i.estimatedHeight,
            "node_subversion": i.zcashdSubversion,
            "node_build": i.zcashdBuild,
            "protocol_version": i.lightwalletProtocolVersion,
            "git_commit": i.gitCommit,
            "upgrade_name": i.upgradeName,
            "upgrade_height": i.upgradeHeight,
            "taddr_support": i.taddrSupport,
        }
    finally:
        ch.close()


def zone_latency(endpoint: str, samples: int = 7, timeout: float = 10.0) -> float | None:
    """Median GetLightdInfo latency on a reused connection, first call ignored.

    This is the gist's method. zec.rocks uses anycast, so TCP connect time
    shows only the distance to the nearest edge; a gRPC call includes the
    trip to the backend, which is what differs between zones.
    """
    ch = open_channel(endpoint)
    try:
        call = svc_grpc.CompactTxStreamerStub(ch).GetLightdInfo
        call(svc.Empty(), timeout=timeout)
        times = []
        for _ in range(samples):
            t0 = time.perf_counter()
            call(svc.Empty(), timeout=timeout)
            times.append((time.perf_counter() - t0) * 1000)
        return round(statistics.median(times), 1)
    except grpc.RpcError:
        return None
    finally:
        ch.close()


def pick_zecrocks_zone(zones: list[str] | None = None) -> tuple[str, dict]:
    """Return (zone, {zone: median ms or None}). Regional zones only by default."""
    zones = zones or [z for z in ZECROCKS_ZONES if z != "global"]
    with ThreadPoolExecutor(max_workers=len(zones)) as pool:
        results = dict(zip(zones, pool.map(lambda z: zone_latency(ZECROCKS_ZONES[z]), zones)))
    usable = {z: v for z, v in results.items() if v is not None}
    if not usable:
        raise RuntimeError("no zec.rocks zone answered")
    return min(usable, key=usable.get), results


def resolve(names: list[str], zecrocks_zone: str = "auto", extra: dict[str, str] | None = None,
            own_extra: set[str] | None = None) -> tuple[list[Service], dict]:
    """Turn service names into endpoints. Returns (services, selection metadata).

    Names: `local` (a server on this machine, own), the presets `zcashlabs`,
    `zecrocks` (zone from `zecrocks_zone`, or `auto` for the lowest-latency
    zone), `fastz` and `fastz-<region>` (all third-party), and every name in
    `extra` (name -> endpoint, from `--endpoint`). `own_extra` (from `--own`)
    marks `extra` names as own. A preset cannot be redefined or marked own.
    """
    extra = extra or {}
    meta: dict = {}
    out: list[Service] = []
    own_extra = own_extra or set()
    for name in names:
        if any(s.name == name for s in out):
            continue
        if name in extra:
            if name in PRESETS:
                raise ValueError("--endpoint cannot redefine the preset %r" % name)
            out.append(Service(name, extra[name], "from --endpoint", own=name in own_extra or name in OWN))
        elif name == "local":
            out.append(Service("local", LOCAL, "server on this machine", own=True))
        elif name == "fastz":
            out.append(Service("fastz", FASTZ, "fastz cluster, region chosen by DNS"))
        elif name.startswith("fastz-") and name[6:] in FASTZ_REGIONS:
            out.append(Service(name, FASTZ_REGIONS[name[6:]], "fastz region %s" % name[6:]))
        elif name == "zcashlabs":
            out.append(Service("zcashlabs", ZCASHLABS))
        elif name == "zecrocks":
            zone = zecrocks_zone
            if zone == "auto":
                zone, lat = pick_zecrocks_zone()
                meta["zecrocks_zone_latency_ms"] = lat
                meta["zecrocks_zone_method"] = "median of 7 GetLightdInfo after one warm-up call (gist method)"
            meta["zecrocks_zone"] = zone
            out.append(Service("zecrocks", ZECROCKS_ZONES[zone], "zone %s" % zone))
        else:
            raise ValueError("unknown service %r; use --endpoint %s=HOST:PORT to define it" % (name, name))
    return out, meta
