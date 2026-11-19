"""Offline tests for the geo sweep: city selection, the DNS client, the per-city analysis and phase-2
choices, and one measured city against in-process fake servers (no VPN, no network)."""

from __future__ import annotations

import json
import socket
import struct
import time
from concurrent import futures

import grpc
import pytest

from fake_server import Fake
from zcash_walletsync_bench import cli, geosweep as gs
from zcash_walletsync_bench.client import ADDRESSES, Client, Recorder
from zcash_walletsync_bench.proto import service_pb2_grpc as svc_grpc


def logical(name, cc, city, *, host=None, features=0, tier=2, status=1, score=1.0, load=10, entry=None,
            lat=0.0, lon=0.0):
    d = {"Name": name, "Tier": tier, "Features": features, "EntryCountry": entry or cc, "ExitCountry": cc,
         "City": city, "ExitLocation": {"Latitude": lat, "Longitude": lon}, "Status": status, "Load": load,
         "Score": score, "Servers": [{"Status": status}]}
    if host:
        d["HostCountry"] = host
    return d


@pytest.fixture
def serverlist(tmp_path):
    L = [
        logical("DE#1", "DE", "Frankfurt", score=2.0, lat=50.1, lon=8.7),
        logical("DE#2", "DE", "Frankfurt", score=1.0, lat=50.1, lon=8.7),
        logical("DE#3", "DE", "Offenbach", lat=50.1, lon=8.76),            # 5 km away: thinned
        logical("BR#1", "BR", "São Paulo", host="US", lat=-23.5, lon=-46.6),  # smart routing only
        logical("BR#2", "BR", "São Paulo", lat=-23.5, lon=-46.6),
        logical("AF#1", "AF", "Kabul", host="UK"),                          # virtual location
        logical("CH-JP#1", "JP", "Tokyo", features=1, entry="CH"),          # Secure Core
        logical("JP#9", "JP", "Tokyo", features=2),                         # Tor
        logical("JP#1", "JP", "Tokyo", tier=3),                             # above the tier
        logical("JP#2", "JP", "Osaka", status=0),                           # down
    ]
    p = tmp_path / "serverlist.json"
    p.write_text(json.dumps({"MaxTier": 2, "LogicalServers": L}))
    return p


def test_load_cities_keeps_physical_exits_only(serverlist):
    cities = gs.load_cities(serverlist)
    assert [c.key for c in cities] == ["br-sao-paulo", "de-frankfurt", "de-offenbach"]
    fra = next(c for c in cities if c.key == "de-frankfurt")
    assert [s[0] for s in fra.servers] == ["DE#2", "DE#1"]  # lower score first
    assert [s[0] for s in cities[0].servers] == ["BR#2"]
    kept = gs.thin(cities, 150)
    assert [c.key for c in kept] == ["br-sao-paulo", "de-frankfurt"]  # Frankfurt has more servers
    assert [c.key for c in gs.select(cities, "de", "offenbach")] == ["de-frankfurt"]


def _reply(qid, name, records):
    """A DNS reply with the question, a CNAME, and the given (type, rdata) answers using compression."""
    q = gs._qname(name)
    out = struct.pack(">HHHHHH", qid, 0x8180, 1, len(records) + 1, 0, 0) + q + struct.pack(">HH", 1, 1)
    cname = gs._qname("edge.example.net")
    out += b"\xc0\x0c" + struct.pack(">HHIH", 5, 1, 30, len(cname)) + cname
    for rtype, rdata in records:
        out += b"\xc0\x0c" + struct.pack(">HHIH", rtype, 1, 30, len(rdata)) + rdata
    return out


def test_dns_parse():
    buf = _reply(7, "rpc.fastz.cash", [(1, socket.inet_aton("15.204.107.12")), (1, socket.inet_aton("1.2.3.4"))])
    assert gs.parse_response(buf, 7, "A") == ["15.204.107.12", "1.2.3.4"]
    txt = bytes([9]) + b"10.2.0.77" + bytes([3]) + b"abc"
    assert gs.parse_response(_reply(9, "o-o.myaddr.l.google.com", [(16, txt)]), 9, "TXT") == ["10.2.0.77abc"]
    with pytest.raises(ValueError):
        gs.parse_response(buf, 8, "A")
    q = gs.build_query("a.b", "A", 5)
    assert q[:2] == b"\x00\x05" and q.endswith(b"\x01a\x01b\x00\x00\x01\x00\x01")


TARGETS = [gs.Target("fastz", "fastz", "x:1"), gs.Target("fastz-eu", "fastz-region", "x:1"),
           gs.Target("fastz-usw", "fastz-region", "x:1"), gs.Target("zecrocks-eu", "zecrocks", "x:1"),
           gs.Target("zecrocks-na", "zecrocks", "x:1"), gs.Target("zcashlabs", "zcashlabs", "x:1")]


def fake_rounds(ms: dict, n=24, jitter=0.5):
    out = []
    for i in range(n):
        j = (i % 3 - 1) * jitter
        out.append({"baseline_ms": 20.0, "ms": {k: v + j for k, v in ms.items()}, "err": {}})
    return out


def test_summarize_city_and_phase2_choice():
    # Competitor 5 ms faster than fastz as DNS steers it, but the best fastz region is faster still.
    rec = {"ok": True, "city": "us-x", "server": "US#1", "fastz_dns_region": "eu",
           "rounds": fake_rounds({"fastz": 50, "fastz-eu": 50, "fastz-usw": 30, "zecrocks-eu": 60,
                                  "zecrocks-na": 45, "zcashlabs": 80})}
    s = gs.summarize_city(rec, TARGETS)
    assert s["fastz_best"] == "fastz-usw"
    assert s["competitors"]["zecrocks"]["target"] == "zecrocks-na"
    assert s["competitors"]["zecrocks"]["vs_fastz"]["verdict"] == "faster"
    assert s["competitors"]["zecrocks"]["vs_fastz_best"]["verdict"] == "slower"
    assert s["competitors"]["zcashlabs"]["vs_fastz"]["verdict"] == "slower"
    assert s["beats_fastz_dns"] == ["zecrocks"] and s["beats_fastz_best"] == [] and s["qualifies"]
    assert s["steering"]["dns_vs_best"]["verdict"] == "slower" and s["steering"]["misteered"]
    assert s["targets"]["fastz"]["p50_minus_tunnel"] == 30.0
    svcs, zone = gs.sync_services(s, "competitor faster")
    assert svcs == ["fastz", "fastz-usw", "zecrocks"] and zone == "na"

    tie = {"ok": True, "city": "de-y", "server": "DE#1", "fastz_dns_region": "eu",
           "rounds": fake_rounds({"fastz": 10, "fastz-eu": 10, "fastz-usw": 160, "zecrocks-eu": 10.3,
                                  "zecrocks-na": 100, "zcashlabs": 30})}
    assert not gs.summarize_city(tie, TARGETS)["qualifies"]
    win = {"ok": True, "city": "sg-z", "server": "SG#1", "fastz_dns_region": "eu",
           "rounds": fake_rounds({"fastz": 10, "fastz-eu": 10, "fastz-usw": 160, "zecrocks-eu": 14,
                                  "zecrocks-na": 100, "zcashlabs": 30})}
    picks = gs.pick_sync_cities([rec, tie, win], TARGETS, n=5, controls=1, margin_ms=1.0)
    assert [(p[0]["city"], p[2]) for p in picks] == [("us-x", "competitor faster"), ("sg-z", "control")]
    svcs, zone = gs.sync_services(picks[1][1], "control")
    assert svcs == ["fastz", "zecrocks"] and zone == "eu"


class SlowTip(Fake):
    def __init__(self, delay_s, **kw):
        super().__init__(**kw)
        self.delay_s = delay_s

    def GetLatestBlock(self, req, ctx):
        time.sleep(self.delay_s)
        return super().GetLatestBlock(req, ctx)


def _serve(fake):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    svc_grpc.add_CompactTxStreamerServicer_to_server(fake, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    return port, server


def test_measure_city_against_fake_servers():
    servers, targets = [], []
    for name, group, delay in (("fastz", "fastz", 0.002), ("fastz-eu", "fastz-region", 0.002),
                               ("zecrocks-eu", "zecrocks", 0.012), ("zcashlabs", "zcashlabs", 0.025)):
        port, srv = _serve(SlowTip(delay))
        servers.append(srv)
        targets.append(gs.Target(name, group, "plain://127.0.0.1:%d" % port))
    queries = []

    def dns(server, name, qtype):
        queries.append((server, name, qtype))
        if qtype == "TXT":
            return ["10.2.0.9", "edns0-client-subnet 0.0.0.0/0"], 1.0
        return ([] if qtype == "NS" else ["127.0.0.1"]), 0.5

    env = gs.Env(vpn=None, dns=dns, trace=lambda: {"colo": "FRA", "loc": "DE", "ip": "10.2.0.7",
                                                   "ip6": "2a02:6ea0:c90f:1::7"},
                 dns_server="10.2.0.1")
    city = gs.City("de-frankfurt", "DE", "Frankfurt", 50.1, 8.7, [("DE#1", 10, 1.0)])
    try:
        rec = gs.measure_city(city, "DE#1", targets, env, rounds=10, tcp_samples=1, pause_s=0)
    finally:
        for s in servers:
            s.stop(0)
    assert rec["ok"] and rec["exit"] == {"colo": "FRA", "loc": "DE", "matches": True}
    assert "10.2.0.7" not in json.dumps(rec) and "c90f:1::7" not in json.dumps(rec)  # exit addresses not stored
    assert rec["resolver"]["same_net_as_exit"] is True
    assert len(rec["rounds"]) == 10 and all(len(r["ms"]) == 4 for r in rec["rounds"])
    assert all(q[0] == "10.2.0.1" for q in queries)
    s = gs.summarize_city(rec, targets)
    assert s["targets"]["zcashlabs"]["p50"] > s["targets"]["fastz"]["p50"] + 15
    assert s["competitors"]["zecrocks"]["vs_fastz"]["verdict"] == "slower" and not s["qualifies"]


def test_measure_city_stops_when_vpn_does_not_route():
    env = gs.Env(vpn=None, dns=lambda *a: (["127.0.0.1"], 0.1), trace=lambda: {"loc": "DE", "colo": "FRA"},
                 home_country="DE")
    city = gs.City("jp-tokyo", "JP", "Tokyo", 35.6, 139.8, [("JP#1", 10, 1.0)])
    rec = gs.measure_city(city, "JP#1", TARGETS, env, rounds=2)
    assert rec.get("fatal") and not rec["ok"]


def test_resolve_pins_address(monkeypatch):
    port, srv = _serve(Fake())
    monkeypatch.setitem(ADDRESSES, "resolve", {"lwd.invalid": "127.0.0.1"})
    try:
        c = Client("x", "plain://lwd.invalid:%d" % port, Recorder())
        from zcash_walletsync_bench.proto import service_pb2 as svc
        assert c.unary("GetLatestBlock", svc.ChainSpec()) is not None
        c.close()
    finally:
        srv.stop(0)


def test_geo_plan_cli(serverlist, capsys):
    assert cli.main(["geo-sweep", "--plan", "--serverlist", str(serverlist)]) == 0
    out = capsys.readouterr().out
    assert "Cities: 2" in out and "BR  1  São Paulo" in out and "zecrocks-global" in out


def test_geo_report(tmp_path):
    from zcash_walletsync_bench import georeport
    rows = {"fastz": 50, "fastz-eu": 50, "fastz-usw": 30, "zecrocks-eu": 60, "zecrocks-na": 45, "zcashlabs": 80}
    rec = {"ok": True, "city": "us-x", "name": "X", "country": "US", "server": "US#1", "fastz_dns_region": "eu",
           "exit": {"colo": "SEA", "loc": "US", "matches": True}, "resolver": {"same_net_as_exit": False},
           "rounds": fake_rounds({**{t.name: 100 for t in gs.default_targets()}, **rows})}
    (tmp_path / "cities.jsonl").write_text(json.dumps({"ok": False, "city": "jp-y", "error": "x"}) + "\n" +
                                           json.dumps(rec) + "\n")
    out = georeport.build(tmp_path)
    assert "X, US" in out and "phase 2" in out and "eu (!)" in out
    assert "DNS chose a slower fastz region" in out and "another network than the exit in: X" in out
    s = json.loads((tmp_path / "summary.json").read_text())
    assert s["failed"] == ["jp-y"] and s["cities"][0]["summary"]["qualifies"]


def test_same_net_compares_one_family():
    assert gs.same_net("10.2.0.9", "10.2.0.7") is True
    assert gs.same_net("10.2.1.9", "10.2.0.7") is False
    assert gs.same_net("2a02:6ea0:c90f:5495::20", "2a02:6ea0:c90f:1::7") is True
    assert gs.same_net("2a02:6ea0:c90f:5495::20", "10.2.0.7") is None  # Paris: resolver asks over IPv6
    assert gs.same_net(None, "10.2.0.7") is None


def test_dead_target_is_skipped_after_warmup():
    port, srv = _serve(SlowTip(0.0))
    calls = {"n": 0}

    class Down(Fake):
        def GetLatestBlock(self, req, ctx):
            calls["n"] += 1
            ctx.abort(grpc.StatusCode.UNAVAILABLE, "down")

    dport, dsrv = _serve(Down())
    targets = [gs.Target("fastz", "fastz", "plain://127.0.0.1:%d" % port),
               gs.Target("zecrocks-me", "zecrocks", "plain://127.0.0.1:%d" % dport)]
    env = gs.Env(vpn=None, dns=lambda *a: ([], 0.1), trace=lambda: {"loc": "DE", "colo": "FRA"})
    city = gs.City("de-frankfurt", "DE", "Frankfurt", 50.1, 8.7, [("DE#1", 10, 1.0)])
    try:
        rec = gs.measure_city(city, "DE#1", targets, env, rounds=6, tcp_samples=1, pause_s=0)
    finally:
        srv.stop(0)
        dsrv.stop(0)
    assert rec["warmup_errors"] == {"zecrocks-me": "UNAVAILABLE"}
    assert calls["n"] == 2 and all("zecrocks-me" not in r["ms"] for r in rec["rounds"])


def test_geo_site(tmp_path):
    from zcash_walletsync_bench.geosite import build_geo_site
    base = {t.name: 100 for t in gs.default_targets()}
    win = {"ok": True, "city": "ar-buenos-aires", "name": "Buenos Aires", "country": "AR", "server": "AR#1",
           "fastz_dns_region": "use", "exit": {"colo": "EZE", "loc": "AR", "matches": True},
           "resolver": {"same_net_as_exit": True}, "warmup_errors": {"zecrocks-me": "DEADLINE_EXCEEDED"},
           "rounds": fake_rounds({**base, "fastz": 420, "fastz-use": 420, "zecrocks-sa": 300, "zcashlabs": 420})}
    lose = {**win, "city": "de-frankfurt", "name": "Frankfurt", "country": "DE", "fastz_dns_region": "eu",
            "rounds": fake_rounds({**base, "fastz": 7, "fastz-eu": 7, "zecrocks-eu": 12, "zcashlabs": 30})}
    (tmp_path / "cities.jsonl").write_text("".join(json.dumps(r) + "\n" for r in (win, lose)))
    run = tmp_path / "runs" / "geo-ar-buenos-aires-x"
    run.mkdir(parents=True)
    walls = {"fastz": [2.0, 2.1, 2.2], "zecrocks": [3.0, 3.1, 3.3]}
    steps = {sid: {"scenario": sid.split("/")[0], "per_service": {
        s: {"per_rep": {"wall_s": v}, "rep_stats": {"wall_s": {"p50": v[1]}}, "reps_failed": 0}
        for s, v in walls.items()}} for sid in ("catch-up/vizor/1d", "first-sync/vizor/10000")}
    (run / "summary.json").write_text(json.dumps({"steps": steps}))
    (tmp_path / "syncs.jsonl").write_text(json.dumps({"ok": True, "city": "ar-buenos-aires",
                                                      "why": "competitor faster", "services": ["fastz", "zecrocks"],
                                                      "run_dir": "runs/geo-ar-buenos-aires-x"}) + "\n")
    page = build_geo_site(tmp_path).read_text()
    assert "from 2 cities" in page and "Buenos Aires, AR" in page and "1.48×" in page
    assert "zecrocks-me</code> did not answer" in page
    assert (tmp_path / "site" / "plots" / "latency-delta.svg").exists()
    assert (tmp_path / "site" / "plots" / "sync-vs-latency.svg").exists()


def test_same_region_on_two_connections_is_not_missteering():
    # Copenhagen: rpc.fastz.cash and eu.fastz.cash are one server, 10 ms apart on two connections.
    rec = {"ok": True, "city": "dk-copenhagen", "server": "DK#1", "fastz_dns_region": "eu",
           "rounds": fake_rounds({"fastz": 51, "fastz-eu": 41, "fastz-usw": 150, "zecrocks-eu": 60,
                                  "zecrocks-na": 130, "zcashlabs": 55})}
    st = gs.summarize_city(rec, TARGETS)["steering"]
    assert st["dns_vs_best"]["verdict"] == "slower" and not st["misteered"]


def test_resolve_retry():
    calls = []

    def flaky(server, name, qtype):
        calls.append(name)
        if len(calls) < 3:
            raise socket.timeout("timed out")
        return ["1.2.3.4"], 1.0

    assert gs.resolve_retry(flaky, "10.2.0.1", "eu.zec.rocks") == ["1.2.3.4"] and len(calls) == 3
    with pytest.raises(socket.timeout):
        gs.resolve_retry(lambda *a: (_ for _ in ()).throw(socket.timeout("x")), "10.2.0.1", "x", tries=2)


def test_phase2_cities_spread_over_the_lead():
    recs = []
    for i, lead in enumerate([5, 10, 20, 40, 80, 160]):
        recs.append({"ok": True, "city": "c%d" % i, "server": "X#1", "fastz_dns_region": "eu",
                     "rounds": fake_rounds({"fastz": 200, "fastz-eu": 200, "fastz-usw": 300,
                                            "zecrocks-eu": 200 - lead, "zecrocks-na": 400, "zcashlabs": 400})})
    picks = gs.pick_sync_cities(recs, TARGETS, n=3, controls=0, margin_ms=1.0)
    assert [p[0]["city"] for p in picks] == ["c5", "c3", "c0"]  # largest, middle, smallest lead


def test_repeat_syncs_adds_earlier_cities_with_their_services(tmp_path):
    rec = {"ok": True, "city": "de-berlin", "server": "X#1", "fastz_dns_region": "eu",
           "rounds": fake_rounds({"fastz": 200, "fastz-eu": 200, "fastz-usw": 300, "zecrocks-eu": 210,
                                  "zecrocks-na": 400, "zcashlabs": 400})}
    old = tmp_path / "old"
    old.mkdir()
    lines = [{"city": "de-berlin", "ok": True, "why": "competitor faster", "services": ["fastz", "fastz-eu",
                                                                                          "zecrocks"],
              "zecrocks_zone": "eu", "run_dir": "runs/x"},
             {"city": "jp-tokyo", "ok": True, "why": "control", "services": ["fastz", "zecrocks"]}]
    (old / "syncs.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    out = gs.repeat_picks(old, [rec], TARGETS, 1.0, taken=set())
    assert [(o[0]["city"], o[2], o[3]) for o in out] == [
        ("de-berlin", "repeat (competitor faster)", (["fastz", "fastz-eu", "zecrocks"], "eu"))]
    assert gs.repeat_picks(old, [rec], TARGETS, 1.0, taken={"de-berlin"}) == []
    assert gs.repeat_picks(None, [rec], TARGETS, 1.0, taken=set()) == []


def test_run_has_data(tmp_path):
    assert not gs.run_has_data(tmp_path)
    (tmp_path / "summary.json").write_text(json.dumps({"steps": {}}))
    assert not gs.run_has_data(tmp_path)  # Querétaro: no service answered, nothing ran
    (tmp_path / "summary.json").write_text(json.dumps({"steps": {"catch-up/vizor/1d": {"per_service": {
        "fastz": {"reps": 3, "reps_failed": 0}}}}}))
    assert gs.run_has_data(tmp_path)


def test_best_records_prefers_complete_measurement():
    full = {"ok": True, "city": "de-berlin", "resolved": {"a": ["1"], "b": ["2"]},
            "rounds": [{"ms": {"fastz": 40}, "err": {}}]}
    worse = {"ok": True, "city": "de-berlin", "resolved": {"a": "error: timed out", "b": ["2"]},
             "rounds": [{"ms": {"zecrocks-eu": 20}, "err": {}}]}
    failed = {"ok": False, "city": "de-berlin"}
    assert gs.best_records([full, worse, failed])["de-berlin"] is full
    newer = dict(full, rounds=[{"ms": {"fastz": 41}, "err": {}}])
    assert gs.best_records([full, newer])["de-berlin"] is newer


class FakeShell:
    """protonvpn and nmcli as the sweep sees them: the CLI says Disconnected, but NetworkManager still
    has a connection up until it is taken down with nmcli."""

    def __init__(self, active=("ProtonVPN NO#24:wireguard",)):
        self.active, self.calls = list(active), []

    def __call__(self, args, timeout=60):
        self.calls.append(args)
        if args[:2] == ["protonvpn", "status"]:
            return 0, "Status: Disconnected\n"
        if args[:3] == ["nmcli", "-t", "-f"] and "--active" in args:
            return 0, "".join(a + "\n" for a in self.active) + "Ford-WLAN:802-11-wireless\n"
        if args[:3] == ["nmcli", "connection", "down"]:
            self.active = [a for a in self.active if not a.startswith(args[3] + ":")]
            return 0, "deactivated"
        return 0, ""


def test_disconnect_takes_down_leftover_networkmanager_connection():
    sh = FakeShell()
    vpn = gs.Vpn(run=sh)
    assert vpn.active_connections() == ["ProtonVPN NO#24"]
    assert vpn.disconnect() == ["ProtonVPN NO#24"]
    assert ["nmcli", "connection", "down", "ProtonVPN NO#24"] in sh.calls and vpn.active_connections() == []


def test_preflight_refuses_with_leftover_tunnel():
    import types
    with pytest.raises(SystemExit, match="NetworkManager still has a Proton connection"):
        gs.preflight(gs.Vpn(run=FakeShell()), types.SimpleNamespace(allow_accelerator=False))


def test_failed_connect_is_cleaned_up():
    sh = FakeShell(active=())

    def run(args, timeout=60):
        if args[:2] == ["protonvpn", "connect"]:
            sh.active.append("ProtonVPN NO#24:wireguard")  # it came up although the CLI reports failure
            return 1, "Error: Connection failed."
        return sh(args, timeout)

    st = gs.Vpn(run=run).connect("NO#24", wait_s=0)
    assert not st["connected"] and st["cleaned_up"] == ["ProtonVPN NO#24"] and sh.active == []
