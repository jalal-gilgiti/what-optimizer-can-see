from __future__ import annotations

import random
import statistics

SEED = 20260928
RESAMPLES = 10_000


def quantile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    position = p * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize(values: list[float]) -> dict[str, float | int | bool]:
    rng = random.Random(SEED)
    medians = [statistics.median(rng.choices(values, k=len(values))) for _ in range(RESAMPLES)]
    mean = statistics.fmean(values)
    cv = statistics.pstdev(values) / mean if mean else 0.0
    return {
        "median_ms": statistics.median(values),
        "iqr_ms": quantile(values, 0.75) - quantile(values, 0.25),
        "cv": cv,
        "ci_low_ms": quantile(medians, 0.025),
        "ci_high_ms": quantile(medians, 0.975),
        "trials": len(values),
        "high_variance": cv > 0.10,
    }


def paired_ratio_ci(numerator: list[float], denominator: list[float]) -> dict[str, float]:
    if len(numerator) != len(denominator):
        raise ValueError("paired samples must have equal length")
    rng = random.Random(SEED)
    observed = statistics.median(numerator) / statistics.median(denominator)
    ratios: list[float] = []
    for _ in range(RESAMPLES):
        indices = [rng.randrange(len(numerator)) for _ in numerator]
        a = [numerator[i] for i in indices]
        b = [denominator[i] for i in indices]
        ratios.append(statistics.median(a) / statistics.median(b))
    return {
        "ratio": observed,
        "ci_low": quantile(ratios, 0.025),
        "ci_high": quantile(ratios, 0.975),
    }

