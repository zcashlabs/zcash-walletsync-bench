"""Instrumented CompactTxStreamer client.

Every RPC goes through `Client.unary` or `Client.stream`. They record one
`CallSample` per call into a shared `Recorder`. Responses are received as raw
protobuf bytes (no deserializer inside the timed section), so the timing is
of the network and the server, not of Python message parsing. Callers parse
the bytes after the clock stops.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import statistics
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Iterable

import grpc

SERVICE_PATH = "/cash.z.wallet.sdk.rpc.CompactTxStreamer/"

# A large block can exceed the gRPC default of 4 MiB.
MAX_MESSAGE = 64 * 1024 * 1024

STREAMING = {
    "GetBlockRange",
    "GetBlockRangeNullifiers",
    "GetTaddressTxids",
    "GetTaddressTransactions",
    "GetMempoolTx",
    "GetMempoolStream",
    "GetSubtreeRoots",
    "GetAddressUtxosStream",
}


def _identity(b: bytes) -> bytes:
    return b


def _serialize(message) -> bytes:
    return message.SerializeToString()


class RateLimited(Exception):
    """The server said 429 or RESOURCE_EXHAUSTED. The harness stops that service."""


@dataclass
class CallSample:
    """One RPC. Written as one line of raw/calls.jsonl.gz."""

    call_id: int
    conn_id: int
    service: str
    scenario: str
    method: str
    ok: bool
    started_s: float  # seconds since the run epoch
    latency_ms: float  # call start to final message / status
    ttfb_ms: float | None  # call start to first response message
    messages: int
    bytes: int  # protobuf message bytes received (no HTTP/2 or TLS framing)
    cold: bool  # first RPC on a new connection
    status: str = "OK"  # gRPC status name, or a harness class (SHORT_STREAM, ...)
    detail: str | None = None
    wallet: str | None = None  # simulated wallet (one connection each)
    purpose: str | None = None  # what the call was for, e.g. "batch", "verify", "poll"
    step: str | None = None  # plan step id, e.g. "first-sync/vizor/10000"
    rep: int | None = None  # repetition number within the step
    round: int | None = None  # interleaving round
    connect_ms: float | None = None  # on the cold call only: connection setup time
    timeout_s: float | None = None  # deadline sent with the call
    max_gap_ms: float | None = None  # streams: longest wait between two messages
    stalls: int | None = None  # streams: gaps longer than the stall threshold
    stall_ms: float | None = None  # streams: total time in those gaps

    @property
    def error(self) -> str | None:
        return None if self.ok else self.status


class Recorder:
    """Thread-safe store of call samples, shared by all clients in a run.

    With a `sink` (rawio.RawWriter), every call, every stream message and
    every connection is also written to the raw files as it happens.
    """

    def __init__(self, sink=None, stall_s: float = 2.0, id_base: int = 0, epoch: float | None = None,
                 keep_in_memory: bool = True, on_call=None) -> None:
        self.epoch = time.perf_counter() if epoch is None else epoch
        self._lock = threading.Lock()
        self.samples: list[CallSample] = []
        self.by_conn: dict[int, list[CallSample]] = {}
        self.sink = sink
        self.stall_s = stall_s
        # Worker processes use a distinct id_base so ids stay unique in a run.
        self._next_call = id_base
        self._next_conn = id_base
        self.keep_in_memory = keep_in_memory
        self.on_call = on_call  # live counters (stress)

    def now(self) -> float:
        return time.perf_counter() - self.epoch

    def new_call_id(self) -> int:
        with self._lock:
            self._next_call += 1
            return self._next_call

    def new_conn_id(self) -> int:
        with self._lock:
            self._next_conn += 1
            return self._next_conn

    def add(self, sample: CallSample, arrivals: list | None = None) -> None:
        with self._lock:
            if self.keep_in_memory:
                self.samples.append(sample)
            self.by_conn.setdefault(sample.conn_id, []).append(sample)
        if self.on_call is not None:
            self.on_call(sample)
        if self.sink is not None:
            self.sink.write("calls", asdict(sample))
            if arrivals:
                self.sink.messages(sample.call_id, arrivals)

    def connection(self, record: dict) -> None:
        if self.sink is not None:
            self.sink.write("connections", record)

    def forget(self, conn_id: int) -> None:
        """Drop the in-memory index of one connection (its calls stay on disk)."""
        with self._lock:
            self.by_conn.pop(conn_id, None)

    def clear(self) -> None:
        with self._lock:
            self.samples.clear()
            self.by_conn.clear()

    def select(self, service: str | None = None, scenario: str | None = None, conn_id: int | None = None
               ) -> list[CallSample]:
        if conn_id is not None and service is None and scenario is None:
            with self._lock:
                return list(self.by_conn.get(conn_id, []))
        with self._lock:
            return [
                s
                for s in self.samples
                if (service is None or s.service == service) and (scenario is None or s.scenario == scenario)
                and (conn_id is None or s.conn_id == conn_id)
            ]


def gaps(arrivals: list[tuple[float, int]], stall_s: float) -> tuple[float | None, int, float]:
    """Longest gap between messages, the number of gaps above stall_s, and
    their total length. The wait for the first message is not a gap (it is
    the time to first message)."""
    if len(arrivals) < 2:
        return None, 0, 0.0
    mx, n, tot = 0.0, 0, 0.0
    lim = stall_s * 1000
    prev = arrivals[0][0]
    for t, _ in arrivals[1:]:
        g = t - prev
        prev = t
        if g > mx:
            mx = g
        if g > lim:
            n += 1
            tot += g
    return round(mx, 2), n, round(tot, 2)


def error_class(exc: BaseException) -> tuple[str, str]:
    """Map an exception to a short, stable class name and a detail string."""
    if isinstance(exc, grpc.RpcError):
        code = exc.code() if hasattr(exc, "code") else None
        details = (exc.details() if hasattr(exc, "details") else "") or ""
        name = code.name if code is not None else "RPC_ERROR"
        if "429" in details or name == "RESOURCE_EXHAUSTED":
            return "RATE_LIMITED", details[:200]
        return name, details[:200]
    if isinstance(exc, TimeoutError):
        return "DEADLINE_EXCEEDED", str(exc)[:200]
    return type(exc).__name__, str(exc)[:200]


# Address selection for every connection the harness opens. `resolve` maps a
# host name to a fixed IPv4 address (like curl --resolve); `ipv4` makes every
# other name resolve to IPv4 only, so services with and without IPv6 are
# compared on the same footing. The geo sweep sets both, because the system
# resolver may sit outside the VPN.
ADDRESSES = {"ipv4": False, "resolve": {}}


def address_for(host: str) -> str | None:
    """The IPv4 address to connect to for `host`, or None for normal resolution."""
    if host in ADDRESSES["resolve"]:
        return ADDRESSES["resolve"][host]
    if ADDRESSES["ipv4"]:
        try:
            ipaddress.IPv4Address(host)
            return host
        except ValueError:
            pass
        return socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)[0][4][0]
    return None


def parse_endpoint(endpoint: str) -> tuple[str, int, bool]:
    """Return (host, port, tls). `plain://host:port` selects plaintext (h2c)."""
    tls = True
    if endpoint.startswith("plain://"):
        endpoint, tls = endpoint[len("plain://"):], False
    elif endpoint.startswith("https://"):
        endpoint = endpoint[len("https://"):]
    host, _, port = endpoint.rstrip("/").rpartition(":")
    if not host:
        host, port = endpoint, "443"
    return host, int(port), tls


def is_loopback(endpoint: str) -> bool:
    """True when the endpoint is on this machine."""
    host = parse_endpoint(endpoint)[0].strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# HTTP/2 receive windows. Rust wallets (Vizor, Zingo, librustzcash, WebZjs)
# use tonic, which leaves hyper's client defaults: a fixed 2 MiB stream window
# and 5 MiB connection window, no adaptive (BDP) window
# (hyper-1.11.1 src/proto/h2/client.rs:48-49,86; tonic-0.14.6
# src/transport/channel/endpoint.rs:92-93). On a long path the window limits
# the transfer rate, so the default copies the wallets. "grpc" keeps
# grpc-core's own defaults (64 KiB start, grown by BDP probes).
H2_WINDOW = {"mode": "wallet"}
WALLET_STREAM_WINDOW = 2 * 1024 * 1024


def h2_options(mode: str | None = None) -> list[tuple]:
    mode = mode or H2_WINDOW["mode"]
    if mode == "wallet":
        return [("grpc.http2.lookahead_bytes", WALLET_STREAM_WINDOW), ("grpc.http2.bdp_probe", 0)]
    if mode == "grpc":
        return []
    raise ValueError("unknown h2 window mode %r" % mode)


def open_channel(endpoint: str) -> grpc.Channel:
    host, port, tls = parse_endpoint(endpoint)
    options = h2_options() + [
        ("grpc.max_receive_message_length", MAX_MESSAGE),
        ("grpc.max_send_message_length", MAX_MESSAGE),
        # Ignore any HTTP proxy on the machine, so the timing is of the direct path.
        ("grpc.enable_http_proxy", 0),
        # Each Client gets its own connection, like a separate wallet process.
        ("grpc.use_local_subchannel_pool", 1),
    ]
    ip = address_for(host)
    if ip:
        # Connect to a fixed IPv4 address but keep the name for TLS (SNI and the
        # certificate check) and for the :authority header.
        target = "ipv4:%s:%d" % (ip, port)
        options += [("grpc.ssl_target_name_override", host), ("grpc.default_authority", host)]
    else:
        target = "%s:%d" % (host, port)
    if tls:
        return grpc.secure_channel(target, grpc.ssl_channel_credentials(), options=options)
    return grpc.insecure_channel(target, options=options)


def probe_connection(endpoint: str, samples: int = 3, timeout: float = 10.0) -> dict:
    """Time DNS, TCP connect and TLS handshake with plain sockets.

    This is separate from gRPC so that setup cost is not mixed into RPC
    latency. No address is recorded.
    """
    host, port, tls = parse_endpoint(endpoint)
    dns, tcp, hs, alpn, version, errors = [], [], [], None, None, []
    for _ in range(samples):
        sock = wrapped = None
        try:
            t0 = time.perf_counter()
            ip = address_for(host)
            family, socktype, proto, _c, addr = socket.getaddrinfo(
                ip or host, port, family=socket.AF_INET if ADDRESSES["ipv4"] else 0, type=socket.SOCK_STREAM)[0]
            dns.append((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(timeout)
            sock.connect(addr)
            tcp.append((time.perf_counter() - t0) * 1000)
            if tls:
                ctx = ssl.create_default_context()
                ctx.set_alpn_protocols(["h2"])
                t0 = time.perf_counter()
                wrapped = ctx.wrap_socket(sock, server_hostname=host)
                hs.append((time.perf_counter() - t0) * 1000)
                alpn, version = wrapped.selected_alpn_protocol(), wrapped.version()
                sock = None
        except Exception as exc:  # recorded, never fatal
            errors.append("%s: %s" % (type(exc).__name__, str(exc)[:120]))
        finally:
            for s in (wrapped, sock):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def med(values):
        return round(statistics.median(values), 2) if values else None

    return {
        "samples": samples,
        "dns_ms_p50": med(dns),
        "tcp_connect_ms_p50": med(tcp),
        "tls_handshake_ms_p50": med(hs) if tls else None,
        "tcp_connect_ms_min": round(min(tcp), 2) if tcp else None,
        "tls": tls,
        "tls_version": version,
        "alpn": alpn,
        "errors": errors,
    }


class Client:
    """One gRPC connection to one service, as one wallet would hold it."""

    def __init__(
        self,
        service: str,
        endpoint: str,
        recorder: Recorder,
        scenario: str = "",
        tag: str | None = None,
        connect_timeout: float = 20.0,
        step: str | None = None,
        rep: int | None = None,
        round_: int | None = None,
    ) -> None:
        self.service, self.endpoint, self.recorder = service, endpoint, recorder
        self.scenario, self.tag = scenario, tag
        self.step, self.rep, self.round = step, rep, round_
        self.conn_id = recorder.new_conn_id()
        self.channel = open_channel(endpoint)
        self._calls: dict[str, Callable] = {}
        self._first = True
        self._first_lock = threading.Lock()
        self._tls = threading.local()
        self.closed = False
        started = recorder.now()
        t0 = time.perf_counter()
        try:
            grpc.channel_ready_future(self.channel).result(timeout=connect_timeout)
            self.connect_ms = (time.perf_counter() - t0) * 1000
            self.connect_error = None
        except grpc.FutureTimeoutError:
            self.connect_ms = None
            self.connect_error = "connect timeout after %.0fs" % connect_timeout
        recorder.connection({"conn_id": self.conn_id, "service": service, "scenario": scenario, "step": step,
                             "rep": rep, "round": round_, "wallet": tag, "started_s": round(started, 4),
                             "connect_ms": None if self.connect_ms is None else round(self.connect_ms, 3),
                             "error": self.connect_error, "h2_window": H2_WINDOW["mode"]})

    def _callable(self, method: str):
        fn = self._calls.get(method)
        if fn is None:
            factory = self.channel.unary_stream if method in STREAMING else self.channel.unary_unary
            fn = factory(SERVICE_PATH + method, request_serializer=_serialize, response_deserializer=_identity)
            self._calls[method] = fn
        return fn

    def last_error(self) -> str | None:
        """Error class of the last call made by this thread on this client."""
        return getattr(self._tls, "error", None)

    def _take_cold(self) -> bool:
        with self._first_lock:
            cold, self._first = self._first, False
        return cold

    def _record(self, method, ok, started, latency, ttfb, n, size, cold, exc=None, tag=None, timeout=None,
                arrivals=None):
        err = detail = None
        if exc is not None:
            err, detail = error_class(exc)
        self._tls.error = err
        mg, ns, st = gaps(arrivals, self.recorder.stall_s) if arrivals is not None else (None, None, None)
        self.recorder.add(
            CallSample(
                call_id=self.recorder.new_call_id(),
                conn_id=self.conn_id,
                service=self.service,
                scenario=self.scenario,
                method=method,
                ok=ok,
                started_s=round(started, 4),
                latency_ms=round(latency, 3),
                ttfb_ms=None if ttfb is None else round(ttfb, 3),
                messages=n,
                bytes=size,
                cold=cold,
                status="OK" if err is None else err,
                detail=detail,
                wallet=self.tag,
                purpose=tag,
                step=self.step,
                rep=self.rep,
                round=self.round,
                connect_ms=None if not cold or self.connect_ms is None else round(self.connect_ms, 3),
                timeout_s=timeout,
                max_gap_ms=mg,
                stalls=ns,
                stall_ms=st,
            ),
            arrivals,
        )
        if err == "RATE_LIMITED":
            raise RateLimited(detail)

    def unary(self, method: str, request, timeout: float = 30.0, tag: str | None = None) -> bytes | None:
        """Run a unary call. Return raw response bytes, or None on error."""
        cold = self._take_cold()
        started = self.recorder.now()
        t0 = time.perf_counter()
        try:
            raw = self._callable(method)(request, timeout=timeout)
        except Exception as exc:
            self._record(method, False, started, (time.perf_counter() - t0) * 1000, None, 0, 0, cold, exc, tag,
                         timeout)
            return None
        lat = (time.perf_counter() - t0) * 1000
        self._record(method, True, started, lat, lat, 1, len(raw), cold, None, tag, timeout)
        return raw

    def stream(
        self,
        method: str,
        request,
        timeout: float = 120.0,
        tag: str | None = None,
        limit_s: float | None = None,
        expect: int | None = None,
    ) -> tuple[list[bytes], bool]:
        """Run a server stream. Return (raw messages, ok).

        The arrival time and size of every message go to the raw messages
        file. `limit_s` stops an open-ended stream (mempool) after that many
        seconds and counts it as a success. `expect` flags a stream that ended
        early with fewer messages than requested (class SHORT_STREAM).
        """
        cold = self._take_cold()
        started = self.recorder.now()
        t0 = time.perf_counter()
        out: list[bytes] = []
        arrivals: list[tuple[float, int]] = []
        size, ttfb, call = 0, None, None
        clock = time.perf_counter
        try:
            call = self._callable(method)(request, timeout=timeout)
            if limit_s is not None:
                timer = threading.Timer(limit_s, call.cancel)
                timer.daemon = True
                timer.start()
            for raw in call:
                arrivals.append(((clock() - t0) * 1000, len(raw)))
                out.append(raw)
                size += len(raw)
        except grpc.RpcError as exc:
            if not (limit_s is not None and exc.code() == grpc.StatusCode.CANCELLED):
                ttfb = arrivals[0][0] if arrivals else None
                self._record(method, False, started, (clock() - t0) * 1000, ttfb, len(out), size, cold, exc, tag,
                             timeout, arrivals)
                return out, False
        except Exception as exc:
            if call is not None:
                call.cancel()
            ttfb = arrivals[0][0] if arrivals else None
            self._record(method, False, started, (clock() - t0) * 1000, ttfb, len(out), size, cold, exc, tag,
                         timeout, arrivals)
            return out, False
        lat = (clock() - t0) * 1000
        ttfb = arrivals[0][0] if arrivals else None
        if expect is not None and len(out) < expect:
            exc = _ShortStream("got %d of %d messages" % (len(out), expect))
            self._record(method, False, started, lat, ttfb, len(out), size, cold, exc, tag, timeout, arrivals)
            return out, False
        self._record(method, True, started, lat, ttfb, len(out), size, cold, None, tag, timeout, arrivals)
        return out, True

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.channel.close()


class _ShortStream(Exception):
    pass


_ShortStream.__name__ = "SHORT_STREAM"


def samples_as_dicts(samples: Iterable[CallSample]) -> list[dict]:
    return [asdict(s) for s in samples]
