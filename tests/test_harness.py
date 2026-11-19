"""Offline tests: the harness against in-process fake servers."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import types

import pytest

from fake_server import make_block, make_fixtures, start
from zcash_walletsync_bench import cli, crosscheck, fixtures, rawio, stats
from zcash_walletsync_bench.client import Client, Recorder, RateLimited
from zcash_walletsync_bench.digest import block_digests
from zcash_walletsync_bench.proto import service_pb2 as svc
from zcash_walletsync_bench.scenarios import parse_span
from zcash_walletsync_bench.wallet import PROFILES, split, sync_range


@pytest.fixture
def server():
    ep, fake, srv = start()
    yield ep, fake
    srv.stop(0)


def counts(rec: Recorder) -> dict:
    out = {}
    for s in rec.samples:
        out[s.method] = out.get(s.method, 0) + 1
    return out


# ---------------------------------------------------------------- units

def test_describe_and_bootstrap():
    xs = list(range(1, 101))
    d = stats.describe(xs)
    assert d["n"] == 100 and d["p50"] == 50.5 and d["min"] == 1 and d["max"] == 100
    lo, hi = d["median_ci95"]
    assert lo < 50.5 < hi
    assert stats.describe([]) == {"n": 0}
    assert stats.describe([7.0])["p99"] == 7.0
    assert stats.describe(xs) == stats.describe(xs)  # seeded


def test_ratio_ci_significance():
    import numpy as np
    r = np.random.default_rng(3)
    a, same, slow = r.normal(100, 10, 40), r.normal(100, 10, 40), r.normal(150, 10, 40)
    assert stats.ratio_ci(slow, a)["significant"]
    assert not stats.ratio_ci(same, a)["significant"]
    assert stats.ratio_ci([1, 2], [1, 2])["ci95"] is None  # too few samples
    c = stats.compare_services({"fast": a, "slow": slow}, lower_is_better=True)
    assert c["best"] == "fast" and c["vs_best"]["slow"]["ratio"] > 1.3
    t = stats.compare_services({"fast": [5, 6, 7], "slow": [1, 2, 3]}, lower_is_better=False)
    assert t["best"] == "fast"


def test_gaps_and_stalls():
    from zcash_walletsync_bench.client import gaps
    arr = [(100.0, 10), (150.0, 10), (2900.0, 10), (3000.0, 10), (6500.0, 10)]
    mx, n, tot = gaps(arr, stall_s=2.0)
    assert mx == 3500.0 and n == 2 and tot == 2750.0 + 3500.0
    assert gaps([(5.0, 1)], 2.0) == (None, 0, 0.0)


def test_split_matches_librustzcash_chunking():
    assert split(10, 25, 10) == [(10, 19), (20, 25)]
    assert split(1, 20001, 2000)[-1] == (20001, 20001)
    assert len(split(3548800, 3568800, 2000)) == 11


def test_parse_span():
    assert parse_span("1w") == 8064
    assert parse_span("1d") == 1152
    assert parse_span("6h") == 288
    assert parse_span("500") == 500


def test_base58check_t1_addresses():
    a = fixtures.t1_address(b"\x00" * 20)
    assert a.startswith("t1") and len(a) == 35
    # Decode and verify the checksum.
    n = 0
    for ch in a:
        n = n * 58 + fixtures.B58.index(ch)
    raw = n.to_bytes(26, "big")
    assert raw[:2] == fixtures.T1_PREFIX
    assert hashlib.sha256(hashlib.sha256(raw[:22]).digest()).digest()[:4] == raw[22:]
    assert fixtures.unused_t1("x") == fixtures.unused_t1("x") != fixtures.unused_t1("y")


def test_committed_fixtures_are_mainnet_and_consistent():
    f = fixtures.load()
    assert f["network"] == "main"
    assert len(f["txids"]) >= 20 and all(len(t["txid"]) == 64 for t in f["txids"])
    assert all(a.startswith("t1") for a in f["taddrs_active"] + f["taddrs_unused"])
    lo, hi = f["transparent_range"]
    assert lo < hi <= f["first_sync"]["tip"] + 1000


def test_block_digest_core_vs_aux():
    a, b = make_block(100), make_block(100)
    assert block_digests(a) == block_digests(b)
    b.time += 1  # aux-only change
    assert block_digests(a)["core"] == block_digests(b)["core"]
    assert block_digests(a)["aux"] != block_digests(b)["aux"]
    c = make_block(100, corrupt=True)
    assert block_digests(a)["core"] != block_digests(c)["core"]


def test_crosscheck_finds_odd_one_out():
    good = {"block": {1: {"core": "a", "aux": "x"}, 2: {"core": "b", "aux": "x"}}}
    bad = {"block": {1: {"core": "a", "aux": "y"}, 2: {"core": "WRONG", "aux": "x"}}}
    r = crosscheck.compare({"s1": good, "s2": good, "s3": bad})
    assert not r["ok"]
    assert r["kinds"]["block"]["mismatches"] == 1
    assert r["odd_one_out"] == {"s3": 1}
    assert r["kinds"]["block"]["aux_differences"] == 1
    assert crosscheck.compare({"s1": good, "s2": good})["ok"]


# ---------------------------------------------------------------- client + wallet profiles

def test_vizor_profile_reproduces_recorded_vizor_counts(server):
    """sync-bench recorded a real Vizor cold sync of 20,001 blocks
    (sync-bench/results/2026-11-13/zec.rocks-20k.json): GetBlockRange x11,
    GetTreeState x12, GetSubtreeRoots x3, GetAddressUtxosStream x2."""
    ep, fake = server
    fake.tip0 = 3_568_800
    rec = Recorder()
    c = Client("fake", ep, rec, scenario="t", tag="w")
    r = sync_range(c, PROFILES["vizor"], 3_548_800, 3_568_800, fresh=True, active_taddrs=[], digests={})
    c.close()
    n = counts(rec)
    assert r["ok"] and r["blocks"] == 20_001
    assert n["GetBlockRange"] == 11
    assert n["GetTreeState"] == 12
    assert n["GetSubtreeRoots"] == 3
    assert n["GetAddressUtxosStream"] == 2
    assert r["time_to_first_block_s"] is not None and r["time_to_first_block_s"] <= r["wall_s"]


def test_librustzcash_profile_is_sequential(server):
    ep, fake = server
    rec = Recorder()
    c = Client("fake", ep, rec, scenario="t", tag="w")
    r = sync_range(c, PROFILES["librustzcash"], 470_001, 495_000, fresh=True, active_taddrs=[], digests={})
    c.close()
    n = counts(rec)
    assert r["ok"] and n["GetBlockRange"] == 3 and n["GetTreeState"] == 3 and n["GetAddressUtxosStream"] == 1
    gbr = sorted((s for s in rec.samples if s.method == "GetBlockRange"), key=lambda s: s.started_s)
    for a, b in zip(gbr, gbr[1:]):  # never two block streams at once
        assert b.started_s >= a.started_s + a.latency_ms / 1000 - 0.01


def test_zingo_resumes_after_deadline():
    ep, fake, srv = start(block_delay_s=0.01)
    try:
        prof = dataclasses.replace(PROFILES["zingo"], stream_deadline_s=0.5, resume_pause_s=0.05)
        rec = Recorder()
        c = Client("fake", ep, rec, scenario="t", tag="w")
        r = sync_range(c, prof, 499_801, 499_900, fresh=True, active_taddrs=[], digests={})
        c.close()
        assert r["ok"], r
        assert r["stream_resumes"] >= 1
        errs = [s.error for s in rec.samples if s.method == "GetBlockRange" and not s.ok]
        assert "DEADLINE_EXCEEDED" in errs
        assert r["blocks"] == 100
        # Zingo asks for an empty page after each subtree-root stream.
        assert counts(rec)["GetSubtreeRoots"] == 6
    finally:
        srv.stop(0)


def test_rate_limit_raises():
    ep, fake, srv = start(rate_limit=True)
    try:
        c = Client("fake", ep, Recorder(), scenario="t")
        with pytest.raises(RateLimited):
            c.unary("GetLatestBlock", svc.ChainSpec(), timeout=5)
        c.close()
    finally:
        srv.stop(0)


def test_cold_call_flag_and_bytes(server):
    ep, _ = server
    rec = Recorder()
    c = Client("fake", ep, rec, scenario="t")
    c.unary("GetLatestBlock", svc.ChainSpec())
    c.unary("GetLatestBlock", svc.ChainSpec())
    raws, ok = c.stream("GetBlockRange", svc.BlockRange(start=svc.BlockID(height=10), end=svc.BlockID(height=19)), expect=10)
    c.close()
    assert ok and len(raws) == 10
    assert [s.cold for s in rec.samples] == [True, False, False]
    assert rec.samples[2].bytes == sum(len(r) for r in raws)
    assert rec.samples[2].ttfb_ms <= rec.samples[2].latency_ms


def test_short_stream_is_an_error(server):
    ep, _ = server
    rec = Recorder()
    c = Client("fake", ep, rec, scenario="t")
    _, ok = c.stream("GetBlockRange", svc.BlockRange(start=svc.BlockID(height=10), end=svc.BlockID(height=12)), expect=5)
    c.close()
    assert not ok and rec.samples[0].error == "SHORT_STREAM"


# ---------------------------------------------------------------- end to end

def _e2e_args(tmp_path, servers, f):
    fx = tmp_path / "fx.json"
    fx.write_text(json.dumps(f))
    eps = []
    for name, (ep, _, _) in zip("abc", servers):
        eps += ["--endpoint", "%s=%s" % (name, ep)]
    return ["--services", ",".join("abc"[: len(servers)]), *eps, "--fixtures", str(fx), "--sizes", "300",
            "--behind", "100", "--duration-s", "2.5", "--poll-s", "0.5", "--observe-s", "0.5", "--wallets", "3",
            "--stagger-s", "0.2", "--pause-s", "0", "--app-open-samples", "2", "--txids", "6", "--scale-levels",
            "1,2", "--scale-behind", "100", "--soak-min", "0.08", "--soak-app-open-s", "1", "--soak-catch-up-s", "2",
            "--soak-enhance-s", "1.5", "--out-dir", str(tmp_path), "--no-geo"]


def test_end_to_end_all_scenarios(tmp_path):
    """Three fake services, 2 repetitions of every step; one service corrupts
    a block in the first-sync range. Checks the raw files, the statistics,
    the report rebuild, the plots and that `plan` predicts the request count."""
    f = make_fixtures()
    tip = f["first_sync"]["tip"]
    servers = [start(tip_step_s=1.0), start(tip_step_s=1.0), start(tip_step_s=1.0, corrupt_heights={tip - 5})]
    try:
        args = _e2e_args(tmp_path, servers, f)
        assert cli.main(["run", "all", "--reps", "2", "--label", "t", *args]) == 0
    finally:
        for _, _, srv in servers:
            srv.stop(0)
    run_dir = next(tmp_path.glob("t-*"))
    for rel in list(rawio.FILES.values()) + ["run.json", "summary.json", "report.md", "raw/crosscheck.json"]:
        assert (run_dir / rel).exists(), rel
    calls = rawio.read_jsonl(run_dir, "calls")
    runs = rawio.read_jsonl(run_dir, "runs")
    summary = json.loads((run_dir / "summary.json").read_text())
    steps = summary["steps"]
    expected = {"app-open", "first-sync/vizor/300", "catch-up/vizor/100", "enhance/vizor", "transparent/vizor",
                "tip-follow", "mempool", "mixed/vizor", "scale/vizor/w1", "scale/vizor/w2", "soak"}
    assert set(steps) == expected
    # Every repetition is kept: 2 reps x 3 services per per-service step.
    fs_runs = [r for r in runs if r["step"] == "first-sync/vizor/300"]
    assert len(fs_runs) == 6 and all(r["result"]["ok"] for r in fs_runs)
    # Service order rotates between repetitions.
    orders = {r["rep"]: [] for r in fs_runs}
    for r in sorted(fs_runs, key=lambda r: (r["rep"], r["order_pos"])):
        orders[r["rep"]].append(r["service"])
    assert orders[0] != orders[1]
    # Per-message records: one per block of every GetBlockRange.
    gbr = [c for c in calls if c["method"] == "GetBlockRange" and c["step"] == "first-sync/vizor/300"]
    msgs = rawio.read_messages(run_dir, {c["call_id"] for c in gbr})
    assert sum(len(v) for v in msgs.values()) == sum(c["messages"] for c in gbr) == 6 * 300
    assert all(c["rep"] in (0, 1) and c["conn_id"] and c["timeout_s"] for c in gbr)
    # Statistics.
    fsa = steps["first-sync/vizor/300"]["per_service"]["a"]
    assert fsa["rep_stats"]["wall_s"]["n"] == 2 and len(fsa["per_rep"]["wall_s"]) == 2
    assert fsa["methods"]["GetBlockRange"]["latency_ms"]["n"] >= 1
    assert fsa["block_streams"]["streams"] == 2
    assert "zingo" in fsa["deadlines"] and "vizor" in fsa["deadlines"]
    assert not steps["first-sync/vizor/300"]["crosscheck"]["ok"]
    assert steps["first-sync/vizor/300"]["crosscheck"]["odd_one_out"] == {"c": 1}
    assert steps["enhance/vizor"]["crosscheck"]["ok"]
    assert summary["scale"]["vizor"]["a"]["2"]["aggregate_mbytes_per_s"]["n"] == 2
    assert summary["soak"] and all(summary["soak"][s] for s in "abc")
    assert summary["tips"]["per_service"]["a"]["snapshots"] > 5
    # Plots and report.
    assert (run_dir / "plots" / "tip-lag.png").exists()
    assert any(p.name.startswith("cdf-first-sync") for p in (run_dir / "plots").iterdir())
    assert any(p.name.startswith("load-curve") for p in (run_dir / "plots").iterdir())
    md = (run_dir / "report.md").read_text()
    assert "## first-sync/vizor/300" in md and "MISMATCH" in md and "median 95% CI" in md
    assert "## Load curve" in md and "## Soak" in md
    # `report` rebuilds the same statistics from the raw files.
    before = (run_dir / "summary.json").read_text()
    assert cli.main(["report", str(run_dir)]) == 0
    after = json.loads((run_dir / "summary.json").read_text())
    assert after["steps"]["first-sync/vizor/300"] == json.loads(before)["steps"]["first-sync/vizor/300"]
    # `plan` predicts the exact request count of a per-service step.
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.main(["plan", "first-sync", "--reps", "2", *args])
    row = [l for l in buf.getvalue().splitlines() if l.startswith("first-sync/vizor/300")][0]
    planned_per_rep = int(row.split()[2])
    actual = [c for c in calls if c["step"] == "first-sync/vizor/300" and c["service"] == "a" and c["rep"] == 0]
    assert planned_per_rep == len(actual)
    # No hostname or username in the output.
    import getpass
    import socket
    for p in run_dir.rglob("*.json"):
        text = p.read_text()
        assert socket.gethostname() not in text and getpass.getuser() not in text


def test_scale_respects_per_service_caps(tmp_path):
    f = make_fixtures()
    servers = [start(), start()]
    try:
        args = _e2e_args(tmp_path, servers, f)
        assert cli.main(["run", "scale", *args, "--reps", "1", "--label", "cap", "--scale-levels", "1,2,4",
                         "--max-wallets", "2", "--cap", "a=4"]) == 0
    finally:
        for _, _, srv in servers:
            srv.stop(0)
    run_dir = next(tmp_path.glob("cap-*"))
    runs = rawio.read_jsonl(run_dir, "runs")
    w4 = {r["service"] for r in runs if r["step"] == "scale/vizor/w4"}
    assert w4 == {"a"}  # b is capped at 2 wallets
    assert {r["service"] for r in runs if r["step"] == "scale/vizor/w2"} == {"a", "b"}


def test_wallet_caps_need_the_flag():
    with pytest.raises(SystemExit):
        cli.main(["run", "scale", "--cap", "local=16"])
    with pytest.raises(SystemExit):
        cli.main(["run", "scale", "--max-wallets", "12"])


def test_deadline_accounting_counts_slow_streams(tmp_path):
    """A slow server: Zingo's 20 s stream deadline is scaled down here by
    checking the accounting directly on synthetic call records."""
    from zcash_walletsync_bench.analysis import deadline_hits
    from zcash_walletsync_bench.wallet import PROFILES as P
    profiles = {n: {"deadlines": p.deadlines} for n, p in P.items()}
    calls = [
        {"method": "GetBlockRange", "latency_ms": 25000, "ttfb_ms": 300, "max_gap_ms": 16000},
        {"method": "GetBlockRange", "latency_ms": 5000, "ttfb_ms": 21000, "max_gap_ms": 100},
        {"method": "GetTransaction", "latency_ms": 12000, "ttfb_ms": 12000},
        {"method": "GetTreeState", "latency_ms": 50, "ttfb_ms": 50},
    ]
    h = deadline_hits(calls, profiles)
    assert h["zingo"]["GetBlockRange total > 20s"] == [1, 2]
    assert h["zingo"]["gap between messages > 15s"] == [1, 2]
    assert h["zingo"]["unary > 10s"] == [1, 2]
    assert h["vizor"]["stream first message > 20s"] == [1, 2]
    assert h["vizor"]["unary > 20s"] == [0, 2]
    assert "librustzcash" not in h


def test_subtree_root_hash_order_is_aux_not_core():
    from zcash_walletsync_bench.digest import subtree_roots_digest
    disp = bytes(3) + hashlib.sha256(b"x").digest()[3:]
    a = svc.SubtreeRoot(rootHash=b"r" * 32, completingBlockHash=disp, completingBlockHeight=7).SerializeToString()
    b = svc.SubtreeRoot(rootHash=b"r" * 32, completingBlockHash=disp[::-1], completingBlockHeight=7).SerializeToString()
    da, db = subtree_roots_digest([a]), subtree_roots_digest([b])
    assert da["core"] == db["core"] and da["aux"] == db["aux"]
    assert (da["completing_hash_order"], db["completing_hash_order"]) == ("display", "internal")
    r = crosscheck.compare({"x": {"subtree_roots": {"k": da}}, "y": {"subtree_roots": {"k": db}}})
    assert r["ok"] and r["kinds"]["subtree_roots"]["completing_hash_order"] == {"x": "display", "y": "internal"}


def test_suite_plan_honours_reps_and_sizes(capsys):
    """suite/plan must apply --reps and --sizes, not the fixed defaults."""
    cli.main(["plan", "--reps", "3", "--sizes", "1k,10k", "--soak-min", "0", "--tip-follow-s", "60"])
    out = capsys.readouterr().out
    rows = {l.split()[0]: int(l.split()[1]) for l in out.splitlines()
            if l and l.split()[0].count("/") >= 0 and len(l.split()) > 2 and l.split()[1].isdigit()}
    assert "first-sync/vizor/50000" not in rows
    assert "soak" not in rows
    assert rows["first-sync/vizor/10000"] == 3 and rows["app-open"] == 3 and rows["scale/vizor/w8"] == 3
    cli.main(["plan", "--sizes", "1k", "--soak-min", "0"])
    out = capsys.readouterr().out
    assert "first-sync/zingo/10000" not in out and "first-sync/vizor/1000 " in out


# ---------------------------------------------------------------- services and stress

def test_service_names_and_kinds():
    from zcash_walletsync_bench.services import resolve
    svcs, _ = resolve(["local", "fastz-eu", "zcashlabs", "mine", "other"],
                      extra={"mine": "example.org:443", "other": "example.net:443"}, own_extra={"mine", "fastz-eu"})
    assert [s.name for s in svcs] == ["local", "fastz-eu", "zcashlabs", "mine", "other"]
    assert [s.kind for s in svcs] == ["own", "third-party", "third-party", "own", "third-party"]
    assert svcs[0].endpoint == "plain://127.0.0.1:9067"
    # `local` can get another address and stays own.
    svcs, _ = resolve(["local"], extra={"local": "plain://127.0.0.1:8137"})
    assert svcs[0].endpoint == "plain://127.0.0.1:8137" and svcs[0].own
    with pytest.raises(ValueError):
        resolve(["nowhere"])
    with pytest.raises(ValueError):
        resolve(["zcashlabs"], extra={"zcashlabs": "plain://127.0.0.1:1"})


def _stress_args(tmp_path, servers, own=True, label="s"):
    f = make_fixtures()
    fx = tmp_path / "fx.json"
    fx.write_text(json.dumps(f))
    args = ["stress", "--services", ",".join("ab"[: len(servers)]), "--fixtures", str(fx), "--out-dir", str(tmp_path),
            "--no-geo", "--label", label, "--yes", "--client-procs", "2", "--level-s", "2", "--stress-think-s", "0.05",
            "--stress-grace-s", "5", "--stress-cooldown-s", "0", "--stress-first-sync-blocks", "50", "--pause-s", "0"]
    for name, (ep, _, _) in zip("ab", servers):
        args += ["--endpoint", "%s=%s" % (name, ep)] + (["--own", name] if own else [])
    return args


def test_stress_ramp_end_to_end(tmp_path):
    servers = [start(), start()]
    try:
        assert cli.main(_stress_args(tmp_path, servers) + ["--levels", "1,4,16", "--stop-min-calls", "100000"]) == 0
    finally:
        for _, _, srv in servers:
            srv.stop(0)
    run_dir = next(tmp_path.glob("s-*"))
    runs = [r for r in rawio.read_jsonl(run_dir, "runs") if r["scenario"] == "stress"]
    assert sorted((r["service"], r["result"]["level"]) for r in runs) == [
        ("a", 1), ("a", 4), ("a", 16), ("b", 1), ("b", 4), ("b", 16)]
    assert (run_dir / "raw" / "stress" / "a" / "w16" / "p1" / "calls.jsonl.gz").exists()
    calls = rawio.read_jsonl(run_dir, "calls")
    ids = [c["call_id"] for c in calls]
    assert len(ids) == len(set(ids))  # unique across worker processes
    sessions = rawio.read_jsonl(run_dir, "sessions")
    assert {s["kind"] for s in sessions} >= {"catch-up-1h", "poll"}
    summary = json.loads((run_dir / "summary.json").read_text())
    lv = summary["stress"]["a"]["levels"]
    assert [e["wallets"] for e in lv] == [1, 4, 16]
    top = lv[-1]
    assert top["sessions"] > 0 and top["requests_per_s"] > 0 and top["unary_ms"]["n"] > 0
    assert top["client"]["procs"] == 2 and top["client"]["cpu_pct_total"] is not None
    assert "client_limited" in top["client"] and top["client"]["sched_delay_p99_ms_max"] is not None
    assert "knee" in summary["stress"]["a"]
    assert summary["own"] == {"a": True, "b": True}
    assert (run_dir / "plots" / "stress.png").exists()
    md = (run_dir / "report.md").read_text()
    assert "## Stress" in md and "Knee estimate" in md and "| a | own |" in md
    assert cli.main(["report", str(run_dir)]) == 0


def test_stress_stops_on_error_rate(tmp_path):
    servers = [start(fail_rate=0.5)]
    try:
        assert cli.main(_stress_args(tmp_path, servers, label="e") + [
            "--levels", "1,2,4", "--level-s", "3", "--stop-error-rate", "0.1", "--stop-min-calls", "5"]) == 0
    finally:
        for _, _, srv in servers:
            srv.stop(0)
    run_dir = next(tmp_path.glob("e-*"))
    runs = [r for r in rawio.read_jsonl(run_dir, "runs") if r["scenario"] == "stress"]
    assert len(runs) == 1  # the ramp stopped after the first level
    assert runs[0]["result"]["stopped_early"] and "error rate" in runs[0]["result"]["stop_reason"]
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["stress"]["a"]["levels"][0]["errors_by_status"].get("UNAVAILABLE", 0) > 0


def test_third_party_stress_refused_without_override(tmp_path):
    # Presets are third-party: refused before any connection is made.
    for svc_name in ("zcashlabs", "zecrocks", "fastz-eu"):
        with pytest.raises(SystemExit):
            cli.main(["stress", "--services", svc_name, "--levels", "1,16", "--yes"])
    # --own cannot mark a preset as own.
    with pytest.raises(SystemExit):
        cli.main(["stress", "--services", "zcashlabs", "--own", "zcashlabs", "--levels", "16", "--yes"])
    # A test endpoint that is not marked own is third-party too.
    with pytest.raises(SystemExit):
        cli.main(["stress", "--services", "x", "--endpoint", "x=plain://127.0.0.1:1", "--levels", "1,32", "--yes"])
    with pytest.raises(SystemExit):
        cli.main(["run", "scale", "--cap", "zcashlabs=16", "--allow-16-wallets"])


def test_stress_asks_for_confirmation():
    # pytest's stdin is not a terminal: without --yes the run must refuse.
    with pytest.raises(SystemExit):
        cli.main(["stress", "--services", "local", "--levels", "1"])


def test_stress_plan_prints_levels(capsys):
    assert cli.main(["plan", "stress", "--services", "local", "--levels", "1,64,256"]) == 0
    out = capsys.readouterr().out
    assert "stress/local" in out and "Total:" in out
    assert len([l for l in out.splitlines() if l.startswith("stress/local")]) == 3
