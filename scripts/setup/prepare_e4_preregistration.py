#!/usr/bin/env python3
"""Freeze the E4 execution schedule and analytical expectations before timing."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
H_VALUES = (20, 100, 1_000)
CORRELATIONS = ["positive", "negative", "independent"]
SEED = 42


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def schedule() -> list[dict[str, object]]:
    output = []
    for high in H_VALUES:
        for phase, count in (("warmup", 3), ("measurement", 10)):
            for trial in range(1, count + 1):
                order = list(CORRELATIONS)
                phase_code = 1 if phase == "warmup" else 2
                random.Random(SEED * 1_000_003 + high * 101 + phase_code * 17 + trial).shuffle(order)
                output.append({
                    "block_id": f"h{high}-{phase}-{trial}",
                    "H": high,
                    "trial_phase": phase,
                    "trial_number": trial,
                    "order": order,
                })
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest["dataset_count"] != 9 or not manifest["matched_marginals"]:
        raise RuntimeError("dataset manifest is not ready for preregistration")
    duck_python = ROOT / ".venv-duckdb" / "bin" / "python"
    duckdb_version = subprocess.check_output(
        [str(duck_python), "-c", "import duckdb; print(duckdb.__version__)"], text=True
    ).strip()
    pg_binary = (
        ROOT / "runtime" / "postgresql" / "postgresql-20260829T084826Z"
        / "root" / "usr" / "lib" / "postgresql" / "14" / "bin" / "postgres"
    )
    postgresql_version = subprocess.check_output([str(pg_binary), "--version"], text=True).strip()
    kernel_path = ROOT / "workloads" / "synthetic" / "e4_kernel.py"
    expected = {}
    for high in H_VALUES:
        expected[str(high)] = {
            "W_positive": 10_000 * high // 2,
            "W_negative": 10_000 * 10 // 2,
            "W_independent": 10_000 * (10 + high) // 4,
            "positive_negative_work_ratio": high / 10,
            "M1_q_positive": 2 * high / (10 + high),
            "M1_q_negative": (10 + high) / 20,
            "M1_q_independent": 1.0,
            "M2_q_all": 1.0,
        }
    payload = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "scope": "E4 only: SQLite, DuckDB, PostgreSQL",
        "grid": {
            "R": 10_000,
            "P_Z_1": 0.5,
            "L": 10,
            "H": list(H_VALUES),
            "correlations": CORRELATIONS,
            "seed": SEED,
            "warmups": 3,
            "measurement_repetitions": 10,
            "timeout_seconds": 60,
        },
        "analytical_expectations": expected,
        "dataset_manifest": str(args.manifest.resolve()),
        "dataset_manifest_sha256": sha256(args.manifest),
        "dataset_sha256": {
            f"H={item['H']},{item['correlation']}": item["dataset_sha256"]
            for item in manifest["datasets"]
        },
        "canonical_udf_source": str(kernel_path.resolve()),
        "canonical_udf_source_sha256": sha256(kernel_path),
        "system_versions": {
            "sqlite": sqlite3.sqlite_version,
            "duckdb": duckdb_version,
            "postgresql": postgresql_version,
            "python": sys.version,
        },
        "execution_order_seed": SEED,
        "execution_order_algorithm": (
            "For each H/phase/trial block, shuffle positive/negative/independent with "
            "Random(42*1000003 + H*101 + phase_code*17 + trial); identical schedule per system."
        ),
        "execution_schedule": schedule(),
        "primary_metrics": [
            "exact_work", "observed_work", "udf_invocations", "median_runtime_ms",
            "coefficient_of_variation", "M1_qerror", "M2_qerror",
            "positive_negative_runtime_ratio", "sqrt_kappa_T",
        ],
        "decision": {
            "GO": "all validation passes and runtime direction replicates at H=100,1000 on all systems",
            "CONDITIONAL_GO": "validation passes but variability/fixed overhead weakens timing evidence",
            "NO_GO": "marginal/work/checksum/trial/frozen-artifact validation fails",
        },
        "automatic_reruns": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps({"run_id": args.run_id, "schedule_blocks": len(payload["execution_schedule"]), "status": "FROZEN"}, sort_keys=True))


if __name__ == "__main__":
    main()
