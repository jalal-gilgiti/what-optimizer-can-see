"""Summary statistics for E5-DC timing cells.

Frozen by PREREGISTRATION.md section 9: median, IQR, CV, and a bootstrap 95%
confidence interval of the median with 10,000 resamples and a fixed seed.
"""

from __future__ import annotations

import random
import statistics
from typing import Any

BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20260923
CV_THRESHOLD = 0.10


def quantile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("empty sample")
    if len(ordered) == 1:
        return ordered[0]
    position = p * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def bootstrap_median_ci(values: list[float], seed: int,
                        resamples: int = BOOTSTRAP_RESAMPLES) -> tuple[float, float]:
    rng = random.Random(seed)
    size = len(values)
    medians = [
        statistics.median(rng.choices(values, k=size))
        for _ in range(resamples)
    ]
    return quantile(medians, 0.025), quantile(medians, 0.975)


def summarize(values: list[float], seed: int) -> dict[str, Any]:
    """Median, IQR, CV and bootstrap CI for one timing cell."""
    median = statistics.median(values)
    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
    ci_low, ci_high = bootstrap_median_ci(values, seed)
    cv = (stdev / mean) if mean > 0 else 0.0
    return {
        "median_ms": median,
        "iqr_ms": quantile(values, 0.75) - quantile(values, 0.25),
        "cv": cv,
        "ci_low_ms": ci_low,
        "ci_high_ms": ci_high,
        "high_variance": cv > CV_THRESHOLD,
        "trials": len(values),
    }


def intervals_disjoint(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True when the two bootstrap CIs do not overlap."""
    return a["ci_high_ms"] < b["ci_low_ms"] or b["ci_high_ms"] < a["ci_low_ms"]


def ratio(larger: float, smaller: float) -> float:
    if smaller <= 0:
        return float("inf")
    return larger / smaller
