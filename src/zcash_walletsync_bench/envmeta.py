"""Environment metadata. No IP address, hostname or username is recorded."""

from __future__ import annotations

import os
import platform
import subprocess
import urllib.request
from datetime import datetime, timezone

import grpc
import google.protobuf


def _git(*args: str) -> str | None:
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        return subprocess.run(["git", *args], cwd=here, capture_output=True, text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def cloudflare_site(host: str = "lwd.zcashlabsrpc.dev", timeout: float = 10.0) -> dict:
    """Cloudflare site and country, as the gist records them.

    /cdn-cgi/trace also returns this machine's IP address. Only `colo` and
    `loc` are kept.
    """
    try:
        with urllib.request.urlopen("https://%s/cdn-cgi/trace" % host, timeout=timeout) as r:
            kv = dict(line.split("=", 1) for line in r.read().decode().splitlines() if "=" in line)
        return {"cloudflare_site": kv.get("colo"), "country": kv.get("loc")}
    except OSError as exc:
        return {"cloudflare_site": None, "country": None, "error": str(exc)[:100]}


def collect(geo: bool = True) -> dict:
    meta = {
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "grpcio": grpc.__version__,
        "protobuf": google.protobuf.__version__,
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "logical_cpus": os.cpu_count(),
        "harness_commit": _git("rev-parse", "HEAD"),
        "harness_dirty": bool(_git("status", "--porcelain")),
    }
    if geo:
        meta["location"] = cloudflare_site()
    return meta
