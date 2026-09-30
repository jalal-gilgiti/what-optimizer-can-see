#!/usr/bin/env python3
"""Common runner for controlled, raw-trial-preserving experiments."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = Path(os.environ.get("ARTIFACT_OUTPUT_ROOT", ROOT)).resolve()
sys.path.insert(0, str(ROOT))

from systems.sqlite.adapter import SQLiteAdapter  # noqa: E402
from workloads.synthetic.model import amplification  # noqa: E402


class QueryTimeoutError(TimeoutError):
    pass


def run_with_timeout(adapter: SQLiteAdapter, timeout_seconds: float) -> dict[str, Any]:
    """Enforce a wall timeout on Unix while preserving a portable fallback."""
    if timeout_seconds <= 0:
        raise ValueError("timeout must be positive")
    if not hasattr(signal, "setitimer"):
        return adapter.run_query()

    def expired(_signum: int, _frame: Any) -> None:
        raise QueryTimeoutError(f"query exceeded {timeout_seconds} seconds")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        return adapter.run_query()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def git_commit() -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def load_rows(path: Path) -> list[dict[str, int]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [{key: int(value) for key, value in row.items()} for row in csv.DictReader(handle)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=float, default=60.0, help="Per-query timeout in seconds (recorded; cooperative adapters may enforce it).")
    parser.add_argument("--system", choices=("sqlite", "duckdb"), required=True)
    parser.add_argument("--workload", default="fixed_loop")
    parser.add_argument("--scale", type=int, default=100)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--loop-bounds", type=int, nargs="*", default=[8, 10, 12])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.warmups < 0 or args.repetitions < 1:
        raise SystemExit("warmups must be non-negative and repetitions must be positive")
    if args.system == "duckdb":
        raise SystemExit(
            "DuckDB is installed, but this environment's 0.6.1 Python API lacks "
            "scalar UDF registration; use --system sqlite for this workload."
        )
    if args.workload != "fixed_loop":
        raise SystemExit("the initial executable adapter supports workload=fixed_loop only")

    dataset = args.dataset.resolve()
    rows = load_rows(dataset)
    if len(rows) != args.scale:
        raise SystemExit(f"dataset has {len(rows)} rows but --scale is {args.scale}")
    bounds = tuple(args.loop_bounds)
    lambda_value = amplification(bounds)
    adapter = SQLiteAdapter()
    adapter.setup()
    adapter.load_data(rows)
    adapter.register_udf(bounds)

    config = {
        "experiment_id": args.experiment_id,
        "system": args.system,
        "system_version": adapter.system_version,
        "workload": args.workload,
        "scale": args.scale,
        "seed": args.seed,
        "loop_bounds": list(bounds),
        "Lambda": lambda_value,
        "warmups": args.warmups,
        "repetitions": args.repetitions,
        "timeout_seconds": args.timeout,
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
    }
    configuration_hash = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    timestamp = dt.datetime.now(dt.timezone.utc)
    stamp = timestamp.strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{args.experiment_id}-{stamp}-{uuid.uuid4().hex[:8]}"
    raw_path = OUTPUT_ROOT / "results" / "raw" / f"{run_id}.jsonl"
    plan_path = OUTPUT_ROOT / "results" / "plans" / f"{run_id}.txt"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.parent.mkdir(parents=True, exist_ok=True)

    plan_started = time.perf_counter_ns()
    plan = adapter.explain_query()
    planning_time_ms = (time.perf_counter_ns() - plan_started) / 1_000_000
    plan_path.write_text(plan + "\n", encoding="utf-8")

    records: list[dict[str, Any]] = []
    total = args.warmups + args.repetitions
    try:
        for index in range(total):
            phase = "warmup" if index < args.warmups else "measurement"
            trial_index = index if phase == "warmup" else index - args.warmups
            started_at = dt.datetime.now(dt.timezone.utc).isoformat()
            start = time.perf_counter_ns()
            status = "ok"
            error = None
            try:
                result = run_with_timeout(adapter, args.timeout)
            except QueryTimeoutError as exc:
                result = {"row_count": None, "checksum": None}
                status = "timeout"
                error = repr(exc)
            except Exception as exc:
                result = {"row_count": None, "checksum": None}
                status = "failed"
                error = repr(exc)
            wall_time_ms = (time.perf_counter_ns() - start) / 1_000_000
            if wall_time_ms > args.timeout * 1000 and status == "ok":
                status = "timeout_observed_after_completion"
            metrics = adapter.collect_metrics()
            expected_invocations = args.scale
            expected_iterations = expected_invocations * lambda_value
            if status == "ok" and (
                metrics.get("udf_invocations") != expected_invocations
                or metrics.get("observed_loop_iterations") != expected_iterations
            ):
                status = "semantic_validation_failed"
                error = (
                    f"expected invocations/iterations {expected_invocations}/{expected_iterations}, "
                    f"observed {metrics.get('udf_invocations')}/"
                    f"{metrics.get('observed_loop_iterations')}"
                )
            record = {
                "schema_version": "1.0.0",
                "experiment_id": args.experiment_id,
                "run_id": run_id,
                "trial_id": f"{run_id}-{phase}-{trial_index}",
                "trial_phase": phase,
                "trial_index": trial_index,
                "timestamp_utc": started_at,
                "git_commit": git_commit(),
                "configuration_hash": configuration_hash,
                "system": args.system,
                "system_version": adapter.system_version,
                "language_runtime": f"Python {platform.python_version()}",
                "workload": args.workload,
                "udf_id": "fixed_loop_checksum",
                "udf_type": "pure_deterministic_scalar_python",
                "dataset_scale": args.scale,
                "seed": args.seed,
                "procedural_parameters": {
                    "loop_type": "fixed_nested",
                    "loop_depth": len(bounds),
                    "loop_bounds": list(bounds),
                    "Lambda": lambda_value,
                    "branch_structure": None,
                    "branch_selectivity": None,
                    "branch_loop_correlation": None,
                    "data_access_pattern": "none",
                    "udf_chain_length": 1,
                    "opaque_component": None,
                },
                "relational_parameters": {
                    "input_cardinality": args.scale,
                    "filter_selectivity": 1.0,
                    "join_shape": None,
                    "join_order": None,
                    "statistics_state": "not_collected",
                },
                "representation": {
                    "source_visible": False,
                    "static_structure_visible": False,
                    "relationalized": False,
                    "dataflow_visible": False,
                    "runtime_profile_available": False,
                    "optimizer_stage_first_available": "unknown",
                },
                "decisions": {
                    "costing": "unknown",
                    "udf_placement": "not_controlled",
                    "predicate_placement": "not_applicable",
                    "join_enumeration": "not_applicable",
                    "materialization": "unknown",
                    "fusion": "not_applicable",
                    "vectorization": "unknown",
                    "parallelism": "unknown",
                },
                "measurements": {
                    "wall_time_ms": wall_time_ms,
                    "database_execution_time_ms": None,
                    "planning_time_ms": planning_time_ms,
                    "compilation_time_ms": None,
                    "udf_invocations": metrics.get("udf_invocations"),
                    "expected_udf_invocations": expected_invocations,
                    "expected_loop_iterations": expected_iterations,
                    "effective_loop_iterations": metrics.get("observed_loop_iterations"),
                    "rows_entering_udf": metrics.get("udf_invocations"),
                    "rows_after_filter": result["row_count"],
                    "result_checksum": result["checksum"],
                    "estimated_cost": None,
                    "chosen_plan": plan,
                    "forced_plan": None,
                    "peak_memory_bytes": None,
                },
                "derived_metrics": {"cost_qerror": None, "runtime_ratio": None, "plan_regret": None},
                "provenance": {
                    "procedural_parameters": "exact",
                    "runtime": "profiled",
                    "optimizer_cost": "opaque",
                },
                "cache_state": "mixed",
                "warmup_count": args.warmups,
                "repetition_count": args.repetitions,
                "timeout_seconds": args.timeout,
                "status": status,
                "error": error,
                "raw_output_paths": [str(raw_path), str(plan_path), str(dataset)],
            }
            records.append(record)
    finally:
        adapter.cleanup()

    with raw_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    failed = sum(record["status"] != "ok" for record in records)
    print(json.dumps({"run_id": run_id, "raw_path": str(raw_path), "plan_path": str(plan_path), "trials": len(records), "non_ok": failed}, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
