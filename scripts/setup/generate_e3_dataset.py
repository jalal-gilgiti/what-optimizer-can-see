#!/usr/bin/env python3
"""Generate the exact, run-specific E3 LOW/HIGH dataset and manifest."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
R = 10_000
SEED = 42
LOW = 10
HIGH = 1_000


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def qerror(estimate: float, actual: float) -> float:
    return max(estimate / actual, actual / estimate)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()

    output_dir = ROOT / "workloads" / "synthetic" / "generated" / args.run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    values_rng = random.Random(SEED * 1_000_003)
    rows = [
        {
            "id": row_id,
            "value": values_rng.randint(1, 1_000_000),
            "population": "LOW" if row_id < R // 2 else "HIGH",
            "loop_count": LOW if row_id < R // 2 else HIGH,
        }
        for row_id in range(R)
    ]
    random.Random(SEED * 100_003).shuffle(rows)
    dataset_path = output_dir / f"e3_low_high_r{R}_seed{SEED}.csv"
    with dataset_path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("id", "value", "population", "loop_count"))
        writer.writeheader()
        writer.writerows(rows)

    contexts = []
    for query_id, population in (("Q_all", None), ("Q_low", "LOW"), ("Q_high", "HIGH")):
        selected = rows if population is None else [row for row in rows if row["population"] == population]
        row_count = len(selected)
        true_work = sum(int(row["loop_count"]) for row in selected)
        conditional_mean = true_work / row_count
        global_prediction = row_count * ((LOW + HIGH) / 2)
        conditional_prediction = row_count * conditional_mean
        contexts.append({
            "query_id": query_id,
            "predicate_population": population,
            "expected_rows": row_count,
            "selectivity": row_count / R,
            "e_i_global": (LOW + HIGH) / 2,
            "e_i_given_query": conditional_mean,
            "true_work": true_work,
            "m1_global_predicted_work": global_prediction,
            "m1_global_qerror": qerror(global_prediction, true_work),
            "m2_conditioned_predicted_work": conditional_prediction,
            "m2_conditioned_qerror": qerror(conditional_prediction, true_work),
        })

    expected = {
        "Q_all": (10_000, 5_050_000, 1.0),
        "Q_low": (5_000, 50_000, 50.5),
        "Q_high": (5_000, 5_000_000, 5_000_000 / 2_525_000),
    }
    for item in contexts:
        rows_expected, work_expected, q_expected = expected[item["query_id"]]
        if item["expected_rows"] != rows_expected or item["true_work"] != work_expected:
            raise RuntimeError(f"exact dataset validation failed: {item['query_id']}")
        if abs(item["m1_global_qerror"] - q_expected) > 1e-12 or item["m2_conditioned_qerror"] != 1.0:
            raise RuntimeError(f"analytical validation failed: {item['query_id']}")

    manifest = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "generator": str(Path(__file__).resolve()),
        "generator_sha256": sha256(Path(__file__).resolve()),
        "algorithm": (
            "Create IDs 0..9999; assign IDs 0..4999 to LOW/count=10 and IDs "
            "5000..9999 to HIGH/count=1000; generate deterministic values; shuffle complete rows "
            "with Random(42*100003); write once and freeze by SHA-256."
        ),
        "dataset_count": 1,
        "dataset_path": str(dataset_path.resolve()),
        "dataset_sha256": sha256(dataset_path),
        "R": R,
        "seed": SEED,
        "schema": {"id": "integer", "value": "integer", "population": "text", "loop_count": "integer"},
        "population_counts": {"LOW": 5_000, "HIGH": 5_000},
        "loop_count_counts": {str(LOW): 5_000, str(HIGH): 5_000},
        "global_e_i": (LOW + HIGH) / 2,
        "contexts": contexts,
        "status": "FROZEN",
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.manifest.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps({"run_id": args.run_id, "dataset_count": 1, "sha256": manifest["dataset_sha256"], "status": "FROZEN"}, sort_keys=True))


if __name__ == "__main__":
    main()
