#!/usr/bin/env python3
"""Aggregate measurement trials without editing or discarding raw observations."""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("ARTIFACT_OUTPUT_ROOT", Path(__file__).resolve().parents[2])).resolve()


def qerror(estimated: float | None, actual: float | None) -> float | None:
    if estimated is None or actual is None or estimated <= 0 or actual <= 0:
        return None
    return max(estimated / actual, actual / estimated)


def plan_regret(chosen_runtime: float | None, fastest_measured_legal: float | None) -> float | None:
    if chosen_runtime is None or fastest_measured_legal is None or fastest_measured_legal <= 0:
        return None
    return chosen_runtime / fastest_measured_legal


def bootstrap_mean_interval(values: list[float], seed: int, samples: int = 10_000) -> list[float] | None:
    if not values:
        return None
    rng = random.Random(seed)
    size = len(values)
    means = sorted(statistics.fmean(rng.choice(values) for _ in range(size)) for _ in range(samples))
    return [means[int(0.025 * (samples - 1))], means[int(0.975 * (samples - 1))]]


def summarize(values: list[float], seed: int) -> dict[str, Any]:
    mean = statistics.fmean(values)
    stddev = statistics.stdev(values) if len(values) > 1 else 0.0
    return {
        "count": len(values),
        "mean_wall_time_ms": mean,
        "median_wall_time_ms": statistics.median(values),
        "stddev_wall_time_ms": stddev,
        "coefficient_of_variation": stddev / mean if mean else None,
        "bootstrap_95_percent_mean_interval_ms": bootstrap_mean_interval(values, seed),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "results" / "raw")
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "normalized" / "summary.json")
    parser.add_argument("--seed", type=int, default=20260829)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = sorted(args.input.glob("*.jsonl")) if args.input.is_dir() else [args.input]
    groups: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    source_paths: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    skipped = defaultdict(int)
    for path in paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("trial_phase") != "measurement":
                skipped["warmup"] += 1
                continue
            if record.get("status") != "ok":
                skipped[record.get("status", "unknown_status")] += 1
                continue
            cache_state = record.get("cache_state", "unknown")
            key = (record["experiment_id"], record["configuration_hash"], cache_state)
            groups[key].append(float(record["measurements"]["wall_time_ms"]))
            source_paths[key].add(str(path))

    summaries = []
    for key, values in sorted(groups.items()):
        experiment_id, configuration_hash, cache_state = key
        item = {
            "experiment_id": experiment_id,
            "configuration_hash": configuration_hash,
            "cache_state": cache_state,
            "source_raw_files": sorted(source_paths[key]),
        }
        item.update(summarize(values, args.seed))
        summaries.append(item)

    output = {
        "schema_version": "1.0.0",
        "aggregation_policy": "Only status=ok measurement trials; groups never mix configuration hashes or cache states.",
        "skipped_records": dict(skipped),
        "summaries": summaries,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    figure_path = ROOT / "results" / "figures" / "median_runtime.png"
    if summaries:
        try:
            import matplotlib.pyplot as plt

            labels = [item["experiment_id"] for item in summaries]
            medians = [item["median_wall_time_ms"] for item in summaries]
            figure, axis = plt.subplots(figsize=(max(5, len(labels) * 1.2), 3.5))
            axis.bar(labels, medians)
            axis.set_ylabel("Median wall time (ms)")
            axis.set_title("Smoke/diagnostic results; not a scientific conclusion")
            figure.tight_layout()
            figure.savefig(figure_path, dpi=160)
            plt.close(figure)
        except Exception as exc:
            print(f"figure not generated: {exc}")
    print(f"wrote {args.output} with {len(summaries)} aggregate(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
