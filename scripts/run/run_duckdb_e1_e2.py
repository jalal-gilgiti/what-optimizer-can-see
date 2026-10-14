#!/usr/bin/env python3
"""Replicate frozen SQLite E1/E2 definitions on pinned DuckDB 1.5.5."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import platform
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

from systems.duckdb.experimental_adapter import DuckDBExperimentalAdapter  # noqa: E402
from workloads.synthetic.model import amplification  # noqa: E402


# Frozen grids copied exactly from the validated SQLite E1/E2 phase.
E1_ROWS = (100, 1_000, 10_000)
E1_LOOPS = ((1,), (10,), (10, 10), (8, 10, 12), (10, 10, 20))
E2_ROWS = 10_000
E2_LOOPS = E1_LOOPS[:-1]
E2_RHOS = (1.0, 0.5, 0.1, 0.01)
E2_FORMS = ("udf_before_filter", "filter_before_udf")


class TimedExecution:
    def __init__(self, adapter: DuckDBExperimentalAdapter, form: str | None, timeout_seconds: float):
        self.adapter = adapter
        self.form = form
        self.timeout_seconds = timeout_seconds
        self.timed_out = False

    def run(self) -> tuple[dict[str, Any], str, str | None, float]:
        previous = signal.getsignal(signal.SIGALRM)

        def expired(_signum: int, _frame: Any) -> None:
            self.timed_out = True
            raise TimeoutError(f"query exceeded {self.timeout_seconds} seconds")

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, self.timeout_seconds)
        started = time.perf_counter_ns()
        try:
            result = self.adapter.run_query(self.form)
            status, error = "ok", None
        except Exception as exc:
            result = {"row_count": None, "checksum": None}
            status = "timeout" if self.timed_out else "failed"
            error = repr(exc)
        finally:
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        return result, status, error, elapsed_ms


def dataset_path(family: str, rows: int, rho: float) -> Path:
    rho_label = str(rho).replace(".", "p")
    return ROOT / "workloads" / "synthetic" / "generated" / f"{family}_r{rows}_rho{rho_label}_seed42.csv"


def load_frozen_dataset(family: str, rows: int, rho: float) -> tuple[list[dict[str, int]], Path, str]:
    path = dataset_path(family, rows, rho)
    metadata_path = path.with_suffix(path.suffix + ".metadata.json")
    if not path.exists() or not metadata_path.exists():
        raise FileNotFoundError(f"frozen SQLite dataset or metadata missing: {path}")
    expected_digest = json.loads(metadata_path.read_text(encoding="utf-8"))["dataset_sha256"]
    observed_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected_digest != observed_digest:
        raise RuntimeError(f"frozen dataset checksum mismatch: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        data = [{key: int(value) for key, value in row.items()} for row in csv.DictReader(handle)]
    if len(data) != rows or sum(row["retained"] for row in data) != round(rows * rho):
        raise RuntimeError(f"frozen dataset cardinality/selectivity mismatch: {path}")
    return data, path, observed_digest


def source_hash() -> str:
    paths = (
        Path(__file__).resolve(),
        ROOT / "systems" / "duckdb" / "experimental_adapter.py",
        ROOT / "workloads" / "synthetic" / "model.py",
        ROOT / "workloads" / "synthetic" / "generate.py",
        ROOT / "configs" / "systems" / "duckdb-requirements.txt",
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def configuration_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def run_configuration(
    *,
    family: str,
    family_run_id: str,
    raw_handle: Any,
    raw_path: Path,
    data: list[dict[str, int]],
    data_path: Path,
    data_sha256: str,
    rows: int,
    bounds: tuple[int, ...],
    rho: float,
    form: str | None,
    warmups: int,
    repetitions: int,
    timeout_seconds: float,
    source_digest: str,
) -> list[dict[str, Any]]:
    lambda_value = amplification(bounds)
    retained = sum(row["retained"] for row in data)
    expected_invocations = rows if form in (None, "udf_before_filter") else retained
    expected_iterations = expected_invocations * lambda_value
    form_label = form or "single_form"
    config_id = f"{family.lower()}-duckdb-r{rows}-rho{rho:g}-l{lambda_value}-{form_label}"
    config = {
        "family": family,
        "system": "duckdb",
        "system_version": duckdb.__version__,
        "R": rows,
        "rho": rho,
        "loop_bounds": list(bounds),
        "Lambda": lambda_value,
        "form": form,
        "seed": 42,
        "warmups": warmups,
        "repetitions": repetitions,
        "timeout_seconds": timeout_seconds,
        "dataset_sha256": data_sha256,
        "source_hash": source_digest,
        "e2_barrier": "OFFSET 0" if form == "udf_before_filter" else None,
    }
    config_hash = configuration_hash(config)
    adapter = DuckDBExperimentalAdapter()
    adapter.setup()
    adapter.load_data_path(data_path)
    adapter.register_udf(bounds)
    query = adapter.E2_QUERIES[form] if form else adapter.QUERY
    plan_started = time.perf_counter_ns()
    plan = adapter.explain_query(form)
    planning_time_ms = (time.perf_counter_ns() - plan_started) / 1_000_000
    plan_path = ROOT / "results" / "plans" / f"{family_run_id}-{config_id}.txt"
    plan_path.write_text(f"QUERY\n{query}\n\nEXPLAIN\n{plan}\n", encoding="utf-8")

    records = []
    try:
        for index in range(warmups + repetitions):
            phase = "warmup" if index < warmups else "measurement"
            trial_number = index + 1 if phase == "warmup" else index - warmups + 1
            result, status, error, wall_time_ms = TimedExecution(
                adapter, form, timeout_seconds
            ).run()
            metrics = adapter.collect_metrics()
            if status == "ok" and (
                metrics["udf_invocations"] != expected_invocations
                or metrics["observed_loop_iterations"] != expected_iterations
            ):
                status = "semantic_validation_failed"
                error = (
                    f"expected invocations/iterations {expected_invocations}/{expected_iterations}; "
                    f"observed {metrics['udf_invocations']}/{metrics['observed_loop_iterations']}"
                )
            record = {
                "schema_version": "1.1.0",
                "experiment_family": family,
                "experiment_id": config_id,
                "configuration_id": config_id,
                "run_id": family_run_id,
                "trial_id": f"{family_run_id}-{config_id}-{phase}-{trial_number}",
                "research_question": (
                    "Does measured procedural work scale with R and Lambda?"
                    if family == "E1" else
                    "How does selective relational placement change controlled procedural work?"
                ),
                "system": "duckdb",
                "system_version": duckdb.__version__,
                "language_runtime": f"Python {platform.python_version()}",
                "environment_path": str(ROOT / ".venv-duckdb"),
                "workload": "fixed_loop" if family == "E1" else "relational_selectivity_fixed_loop",
                "udf_id": "fixed_loop_checksum_observed",
                "udf_type": "pure_deterministic_scalar_python_with_internal_counter",
                "dataset_scale": rows,
                "R": rows,
                "rho": rho,
                "form": form,
                "seed": 42,
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
                    "input_cardinality": rows,
                    "filter_selectivity_requested": rho,
                    "filter_selectivity_realized": (
                        result["row_count"] / rows if result["row_count"] is not None else None
                    ),
                    "join_shape": None,
                    "join_order": None,
                    "statistics_state": "default",
                },
                "representation": {
                    "source_visible": False,
                    "static_structure_visible": False,
                    "relationalized": False,
                    "dataflow_visible": False,
                    "runtime_profile_available": False,
                    "optimizer_stage_first_available": None,
                },
                "decisions": {
                    "costing": None,
                    "udf_placement": "explicit_controlled_form" if form else "not_applicable",
                    "predicate_placement": form or "not_applicable",
                    "join_enumeration": "not_applicable",
                    "materialization": "not_forced",
                    "evaluation_barrier": (
                        "offset_zero_streaming_limit" if form == "udf_before_filter" else "none"
                    ),
                    "fusion": None,
                    "vectorization": "native_python_scalar_callback",
                    "parallelism": "duckdb_default",
                },
                "trial_phase": phase,
                "trial_number": trial_number,
                "cache_state": (
                    "connection_cold_os_uncontrolled"
                    if phase == "warmup" and index == 0 else
                    "connection_warm_os_uncontrolled"
                ),
                "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "configuration_hash": config_hash,
                "source_hash": source_digest,
                "git_commit": None,
                "measurements": {
                    "expected_udf_invocations": expected_invocations,
                    "udf_invocations": metrics["udf_invocations"],
                    "expected_loop_iterations": expected_iterations,
                    "effective_loop_iterations": metrics["observed_loop_iterations"],
                    "observed_loop_iterations": metrics["observed_loop_iterations"],
                    "rows_entering_udf": metrics["udf_invocations"],
                    "rows_after_filter": result["row_count"],
                    "result_checksum": result["checksum"],
                    "wall_time_ms": wall_time_ms,
                    "database_execution_time_ms": None,
                    "planning_time_ms": planning_time_ms,
                    "compilation_time_ms": None,
                    "estimated_cost": None,
                    "query_plan": plan,
                    "query_text": query,
                    "chosen_plan": None,
                    "forced_plan": form,
                    "peak_memory_bytes": None,
                },
                "derived_metrics": {
                    "cost_qerror": None, "runtime_ratio": None, "plan_regret": None
                },
                "provenance": {
                    "loop_structure": "exact",
                    "observed_iterations": "profiled",
                    "runtime": "profiled",
                    "optimizer_cost": "opaque",
                },
                "warmup_count": warmups,
                "repetition_count": repetitions,
                "timeout_seconds": timeout_seconds,
                "status": status,
                "error": error,
                "raw_output_paths": [str(raw_path), str(plan_path), str(data_path)],
                "dataset_sha256": data_sha256,
            }
            raw_handle.write(json.dumps(record, sort_keys=True) + "\n")
            raw_handle.flush()
            records.append(record)
            if status == "semantic_validation_failed":
                raise RuntimeError(error)
    finally:
        adapter.cleanup()
    return records


def validate_pair(records: list[dict[str, Any]], rho: float, lambda_value: int) -> None:
    by_trial: dict[tuple[str, int], dict[str, int | None]] = {}
    for record in records:
        if record["status"] != "ok":
            continue
        key = (record["trial_phase"], record["trial_number"])
        by_trial.setdefault(key, {})[record["form"]] = record["measurements"]["result_checksum"]
    for key, checksums in by_trial.items():
        if set(checksums) == set(E2_FORMS) and len(set(checksums.values())) != 1:
            raise RuntimeError(
                f"E2 checksum mismatch at rho={rho}, Lambda={lambda_value}, trial={key}: {checksums}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", choices=("E1", "E2", "both"), default="both")
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if duckdb.__version__ != "1.5.5":
        raise SystemExit(f"expected pinned DuckDB 1.5.5, found {duckdb.__version__}")
    if args.warmups < 0 or args.repetitions < 1 or args.timeout <= 0:
        raise SystemExit("invalid warmup, repetition, or timeout value")
    families = ("E1", "E2") if args.family == "both" else (args.family,)
    digest = source_hash()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary = {}

    for family in families:
        run_id = f"{family.lower()}-duckdb-{stamp}-{uuid.uuid4().hex[:8]}"
        raw_path = ROOT / "results" / "raw" / f"{run_id}.jsonl"
        all_records = []
        with raw_path.open("x", encoding="utf-8") as raw_handle:
            if family == "E1":
                for rows in E1_ROWS:
                    data, path, sha256 = load_frozen_dataset("fixed_loop", rows, 1.0)
                    for bounds in E1_LOOPS:
                        print(f"START E1 DuckDB R={rows} Lambda={amplification(bounds)}", flush=True)
                        all_records.extend(run_configuration(
                            family=family, family_run_id=run_id, raw_handle=raw_handle,
                            raw_path=raw_path, data=data, data_path=path, data_sha256=sha256,
                            rows=rows, bounds=bounds, rho=1.0, form=None,
                            warmups=args.warmups, repetitions=args.repetitions,
                            timeout_seconds=args.timeout, source_digest=digest,
                        ))
            else:
                for rho in E2_RHOS:
                    data, path, sha256 = load_frozen_dataset(
                        "relational_selectivity", E2_ROWS, rho
                    )
                    for bounds in E2_LOOPS:
                        pair = []
                        for form in E2_FORMS:
                            print(
                                f"START E2 DuckDB R={E2_ROWS} rho={rho:g} "
                                f"Lambda={amplification(bounds)} form={form}", flush=True,
                            )
                            pair.extend(run_configuration(
                                family=family, family_run_id=run_id, raw_handle=raw_handle,
                                raw_path=raw_path, data=data, data_path=path, data_sha256=sha256,
                                rows=E2_ROWS, bounds=bounds, rho=rho, form=form,
                                warmups=args.warmups, repetitions=args.repetitions,
                                timeout_seconds=args.timeout, source_digest=digest,
                            ))
                        validate_pair(pair, rho, amplification(bounds))
                        all_records.extend(pair)
        summary[family] = {
            "run_id": run_id,
            "raw_path": str(raw_path),
            "records": len(all_records),
            "measurement_trials": sum(r["trial_phase"] == "measurement" for r in all_records),
            "non_ok": sum(r["status"] != "ok" for r in all_records),
        }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
