#!/usr/bin/env python3
"""Freeze E3 analytical expectations and execution schedule before timing."""

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
SEED = 42
QUERIES = ["Q_all", "Q_low", "Q_high"]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def schedule() -> list[dict[str, object]]:
    output = []
    for phase, count in (("warmup", 3), ("measurement", 10)):
        for trial in range(1, count + 1):
            order = list(QUERIES)
            phase_code = 1 if phase == "warmup" else 2
            random.Random(SEED * 1_000_003 + phase_code * 101 + trial).shuffle(order)
            output.append({
                "block_id": f"{phase}-{trial}",
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
    if manifest["run_id"] != args.run_id or manifest["dataset_count"] != 1 or manifest["status"] != "FROZEN":
        raise RuntimeError("dataset manifest is not frozen for this run")
    dataset = Path(manifest["dataset_path"])
    if sha256(dataset) != manifest["dataset_sha256"]:
        raise RuntimeError("dataset SHA-256 mismatch before preregistration")

    duck_python = ROOT / ".venv-duckdb" / "bin" / "python"
    duckdb_version = subprocess.check_output([str(duck_python), "-c", "import duckdb; print(duckdb.__version__)"], text=True).strip()
    pg_binary = ROOT / "runtime/postgresql/postgresql-20260829T084826Z/root/usr/lib/postgresql/14/bin/postgres"
    postgresql_version = subprocess.check_output([str(pg_binary), "--version"], text=True).strip()
    kernel = ROOT / "workloads/synthetic/e3_kernel.py"
    expectations = {item["query_id"]: item for item in manifest["contexts"]}
    payload = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "scope": "E3 only: SQLite, DuckDB, PostgreSQL",
        "grid": {
            "R": 10_000, "LOW": 10, "HIGH": 1_000, "global_e_i": 505,
            "queries": QUERIES, "seed": SEED, "warmups": 3,
            "measurement_repetitions": 10, "timeout_seconds": 60,
        },
        "analytical_expectations": expectations,
        "dataset_manifest": str(args.manifest.resolve()),
        "dataset_manifest_sha256": sha256(args.manifest),
        "dataset_path": str(dataset.resolve()),
        "dataset_sha256": manifest["dataset_sha256"],
        "canonical_udf_source": str(kernel.resolve()),
        "canonical_udf_source_sha256": sha256(kernel),
        "system_versions": {
            "sqlite": sqlite3.sqlite_version,
            "duckdb": duckdb_version,
            "postgresql": postgresql_version,
            "python": sys.version,
        },
        "execution_order_seed": SEED,
        "execution_order_algorithm": (
            "For each phase/trial block shuffle Q_all/Q_low/Q_high with "
            "Random(42*1000003 + phase_code*101 + trial); use the identical schedule per system."
        ),
        "execution_schedule": schedule(),
        "primary_metrics": [
            "selected_rows", "udf_invocations", "effective_loop_iterations",
            "median_runtime_ms", "coefficient_of_variation", "M1_global_qerror",
            "M2_conditioned_qerror", "Q_high/Q_low_runtime_ratio",
        ],
        "decision": {
            "GO": "all semantic/freeze validation passes and Q_high is slower than Q_low on every system",
            "CONDITIONAL_GO": "validation passes but variability or fixed overhead weakens runtime separation",
            "NO_GO": "dataset/work/checksum/trial/frozen-artifact validation fails",
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
