"""Descriptive statistics, bootstrap confidence intervals, and comparisons.

All resampling uses a fixed seed, so the same raw data gives the same report.
"""

from __future__ import annotations

import math

import numpy as np

QUANTILES = (5, 25, 50, 75, 90, 95, 99)
BOOT = 2000
SEED = 20261113


def _arr(values) -> np.ndarray:
    return np.asarray([v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))],
                      dtype=float)


def describe(values, ci: bool = True) -> dict:
    """n, mean, stdev, min, p5..p99, max, and a bootstrap 95% CI of the median."""
    a = _arr(values)
    if a.size == 0:
        return {"n": 0}
    out = {"n": int(a.size), "mean": float(a.mean()), "stdev": float(a.std(ddof=1)) if a.size > 1 else 0.0,
           "min": float(a.min())}
    for q in QUANTILES:
        out["p%d" % q] = float(np.percentile(a, q))
    out["max"] = float(a.max())
    out = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in out.items()}
    if ci and a.size >= 3:
        out["median_ci95"] = [round(x, 4) for x in median_ci(a)]
    return out


def _boot_medians(a: np.ndarray, rng, n: int = BOOT) -> np.ndarray:
    """Medians of n bootstrap resamples, in chunks so memory stays small."""
    chunk = max(1, min(n, 2_000_000 // max(1, a.size)))
    out = []
    left = n
    while left > 0:
        k = min(chunk, left)
        out.append(np.median(a[rng.integers(0, a.size, size=(k, a.size))], axis=1))
        left -= k
    return np.concatenate(out)


def median_ci(a, level: float = 0.95) -> tuple[float, float]:
    """95% CI of the median: bootstrap for n <= 5000, else the
    distribution-free order-statistic interval."""
    a = np.sort(_arr(a))
    n = a.size
    if n > 5000:
        z = 1.959964
        lo = int(max(0, math.floor(n / 2 - z * math.sqrt(n) / 2)))
        hi = int(min(n - 1, math.ceil(n / 2 + z * math.sqrt(n) / 2)))
        return float(a[lo]), float(a[hi])
    return bootstrap_ci(a, level=level)


def bootstrap_ci(a, n: int = BOOT, level: float = 0.95) -> tuple[float, float]:
    a = _arr(a)
    s = _boot_medians(a, np.random.default_rng(SEED), n)
    lo, hi = np.percentile(s, [(1 - level) / 2 * 100, (1 + level) / 2 * 100])
    return float(lo), float(hi)


def ratio_ci(a, b, n: int = BOOT, level: float = 0.95) -> dict:
    """Median(a) / median(b) with a bootstrap CI (independent resampling).

    `significant` is True when the CI does not contain 1. With fewer than 3
    values on a side, no CI is given and the result is not significant.
    """
    a, b = _arr(a), _arr(b)
    if a.size == 0 or b.size == 0:
        return {"ratio": None, "ci95": None, "significant": False, "note": "no data"}
    mb = float(np.median(b))
    ratio = float(np.median(a)) / mb if mb else None
    if a.size < 3 or b.size < 3 or ratio is None:
        return {"ratio": None if ratio is None else round(ratio, 4), "ci95": None, "significant": False,
                "note": "too few samples for a CI"}
    rng = np.random.default_rng(SEED)
    ma = _boot_medians(a, rng, n)
    mbs = _boot_medians(b, rng, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = ma / mbs
    r = r[np.isfinite(r)]
    lo, hi = np.percentile(r, [(1 - level) / 2 * 100, (1 + level) / 2 * 100])
    return {"ratio": round(ratio, 4), "ci95": [round(float(lo), 4), round(float(hi), 4)],
            "significant": bool(lo > 1.0 or hi < 1.0), "p_mannwhitney": mann_whitney_p(a, b)}


def mann_whitney_p(a, b) -> float | None:
    """Two-sided Mann-Whitney U p-value (normal approximation with tie
    correction). None when a side has fewer than 3 values."""
    a, b = _arr(a), _arr(b)
    n1, n2 = a.size, b.size
    if n1 < 3 or n2 < 3:
        return None
    allv = np.concatenate([a, b])
    order = allv.argsort(kind="mergesort")
    ranks = np.empty(allv.size)
    sorted_v = allv[order]
    i = 0
    while i < allv.size:
        j = i
        while j + 1 < allv.size and sorted_v[j + 1] == sorted_v[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1
        i = j + 1
    u1 = ranks[:n1].sum() - n1 * (n1 + 1) / 2.0
    mu = n1 * n2 / 2.0
    _, counts = np.unique(allv, return_counts=True)
    n = n1 + n2
    tie = (counts ** 3 - counts).sum() / (n * (n - 1)) if n > 1 else 0
    sigma = math.sqrt(n1 * n2 / 12.0 * ((n + 1) - tie))
    if sigma == 0:
        return 1.0
    z = (abs(u1 - mu) - 0.5) / sigma
    return round(float(math.erfc(max(z, 0) / math.sqrt(2))), 6)


def compare_services(samples: dict[str, list[float]], lower_is_better: bool = True) -> dict:
    """Each service against the best one (lowest median, or highest when
    lower_is_better is False). Ratio > 1 means worse than the best for
    latency-like metrics, and better for throughput-like ones."""
    meds = {k: float(np.median(_arr(v))) for k, v in samples.items() if _arr(v).size}
    if not meds:
        return {}
    best = (min if lower_is_better else max)(meds, key=meds.get)
    out = {"best": best, "lower_is_better": lower_is_better, "vs_best": {}}
    for k, v in samples.items():
        if k == best or k not in meds:
            continue
        out["vs_best"][k] = ratio_ci(v, samples[best])
    return out
