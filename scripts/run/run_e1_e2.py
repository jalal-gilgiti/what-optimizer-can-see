#!/usr/bin/env python3
"""Run the approved E1 and E2 SQLite matrices with semantic assertions."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import signal
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = Path(os.environ.get("ARTIFACT_OUTPUT_ROOT", ROOT)).resolve()
sys.path.insert(0, str(ROOT))

from systems.sqlite.adapter import SQLiteAdapter  # noqa: E402
from workloads.synthetic.generate import generate, write_dataset  # noqa: E402
from workloads.synthetic.model import amplification, fixed_loop_checksum_observed  # noqa: E402


E1_ROWS = (100, 1_000, 10_000)
E1_LOOPS = ((1,), (10,), (10, 10), (8, 10, 12), (10, 10, 20))
E2_ROWS = 10_000
E2_LOOPS = E1_LOOPS[:-1]
E2_RHOS = (1.0, 0.5, 0.1, 0.01)
E2_FORMS = ("udf_before_filter", "filter_before_udf")


class TimedExecution:
    def __init__(self, adapter: SQLiteAdapter, form: str | None, timeout_seconds: float):
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


def dataset_arguments(rows: int, rho: float, family: str) -> argparse.Namespace:
    return argparse.Namespace(
        rows=rows,
        seed=42,
        loop_bounds=[1],
        distribution="constant",
        dynamic_base=1,
        family=family,
        branch_selectivity=0.5,
        filter_selectivity=rho,
        correlation="independent",
        data_access_pattern="none",
        lookup_keys=16,
        udf_chain_length=1,
    )


def prepare_dataset(rows: int, rho: float, family: str) -> tuple[list[dict[str, int]], Path, str]:
    generated, summary = generate(dataset_arguments(rows, rho, family))
    rho_label = str(rho).replace(".", "p")
    path = OUTPUT_ROOT / "workloads" / "synthetic" / "generated" / f"{family}_r{rows}_rho{rho_label}_seed42.csv"
    write_dataset(generated, summary, path)
    return generated, path, summary["dataset_sha256"]


def source_hash() -> str:
    paths = (
        Path(__file__).resolve(),
        ROOT / "systems" / "sqlite" / "adapter.py",
        ROOT / "workloads" / "synthetic" / "model.py",
        ROOT / "workloads" / "synthetic" / "generate.py",
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def make_configuration_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def cache_state(phase: str, global_index: int) -> str:
    if phase == "warmup" and global_index == 0:
        return "connection_cold_os_uncontrolled"
    return "connection_warm_os_uncontrolled"


def trial_record(
    *,
    family: str,
    family_run_id: str,
    configuration_id: str,
    config_hash: str,
    source_digest: str,
    rows: int,
    bounds: tuple[int, ...],
    rho: float,
    form: str | None,
    phase: str,
    trial_number: int,
    global_index: int,
    expected_invocations: int,
    expected_iterations: int,
    result: dict[str, Any],
    metrics: dict[str, Any],
    status: str,
    error: str | None,
    wall_time_ms: float,
    planning_time_ms: float,
    plan: str,
    query: str,
    raw_path: Path,
    plan_path: Path,
    dataset_path: Path,
    dataset_sha256: str,
    warmups: int,
    repetitions: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    lambda_value = amplification(bounds)
    return {
        "schema_version": "1.1.0",
        "experiment_family": family,
        "experiment_id": configuration_id,
        "run_id": family_run_id,
        "configuration_id": configuration_id,
        "trial_id": f"{family_run_id}-{configuration_id}-{phase}-{trial_number}",
        "research_question": (
            "Does measured procedural work scale with R and Lambda?"
            if family == "E1" else
            "How does selective relational placement change controlled procedural work?"
        ),
        "system": "sqlite",
        "system_version": sqlite3.sqlite_version,
        "language_runtime": f"Python {platform.python_version()}",
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
            "filter_selectivity_realized": result["row_count"] / rows if result["row_count"] is not None else None,
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
            "optimizer_stage_first_available": None,
        },
        "decisions": {
            "costing": None,
            "udf_placement": "explicit_controlled_form" if form else "not_applicable",
            "predicate_placement": form or "not_applicable",
            "join_enumeration": "not_applicable",
            "materialization": "not_forced",
            "evaluation_barrier": (
                "non_flattenable_subquery_limit_offset"
                if form == "udf_before_filter" else "none"
            ),
            "fusion": None,
            "vectorization": None,
            "parallelism": None,
        },
        "trial_phase": phase,
        "trial_number": trial_number,
        "cache_state": cache_state(phase, global_index),
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
        "derived_metrics": {"cost_qerror": None, "runtime_ratio": None, "plan_regret": None},
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
        "raw_output_paths": [str(raw_path), str(plan_path), str(dataset_path)],
        "dataset_sha256": dataset_sha256,
    }


def run_configuration(
    *,
    family: str,
    family_run_id: str,
    raw_handle: Any,
    raw_path: Path,
    rows_data: list[dict[str, int]],
    dataset_path: Path,
    dataset_sha256: str,
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
    retained = sum(row["retained"] for row in rows_data)
    expected_invocations = rows if form in (None, "udf_before_filter") else retained
    expected_iterations = expected_invocations * lambda_value
    form_label = form or "single_form"
    configuration_id = f"{family.lower()}-sqlite-r{rows}-rho{rho:g}-l{lambda_value}-{form_label}"
    config = {
        "family": family,
        "system": "sqlite",
        "system_version": sqlite3.sqlite_version,
        "R": rows,
        "rho": rho,
        "loop_bounds": list(bounds),
        "Lambda": lambda_value,
        "form": form,
        "seed": 42,
        "warmups": warmups,
        "repetitions": repetitions,
        "timeout_seconds": timeout_seconds,
        "dataset_sha256": dataset_sha256,
        "source_hash": source_digest,
    }
    config_hash = make_configuration_hash(config)
    adapter = SQLiteAdapter()
    adapter.setup()
    adapter.load_data(rows_data)
    adapter.register_udf(bounds)
    query = adapter.E2_QUERIES[form] if form else adapter.QUERY
    plan_start = time.perf_counter_ns()
    plan = adapter.explain_query(form)
    planning_time_ms = (time.perf_counter_ns() - plan_start) / 1_000_000
    plan_path = OUTPUT_ROOT / "results" / "plans" / f"{family_run_id}-{configuration_id}.txt"
    plan_path.write_text(f"QUERY\n{query}\n\nEXPLAIN QUERY PLAN\n{plan}\n", encoding="utf-8")

    records = []
    try:
        for index in range(warmups + repetitions):
            phase = "warmup" if index < warmups else "measurement"
            trial_number = index + 1 if phase == "warmup" else index - warmups + 1
            execution = TimedExecution(adapter, form, timeout_seconds)
            result, status, error, wall_time_ms = execution.run()
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
            record = trial_record(
                family=family,
                family_run_id=family_run_id,
                configuration_id=configuration_id,
                config_hash=config_hash,
                source_digest=source_digest,
                rows=rows,
                bounds=bounds,
                rho=rho,
                form=form,
                phase=phase,
                trial_number=trial_number,
                global_index=index,
                expected_invocations=expected_invocations,
                expected_iterations=expected_iterations,
                result=result,
                metrics=metrics,
                status=status,
                error=error,
                wall_time_ms=wall_time_ms,
                planning_time_ms=planning_time_ms,
                plan=plan,
                query=query,
                raw_path=raw_path,
                plan_path=plan_path,
                dataset_path=dataset_path,
                dataset_sha256=dataset_sha256,
                warmups=warmups,
                repetitions=repetitions,
                timeout_seconds=timeout_seconds,
            )
            raw_handle.write(json.dumps(record, sort_keys=True) + "\n")
            raw_handle.flush()
            records.append(record)
            if status == "semantic_validation_failed":
                raise RuntimeError(error)
    finally:
        adapter.cleanup()
    return records


def validate_pair(records: list[dict[str, Any]], rho: float, lambda_value: int) -> None:
    successful = [record for record in records if record["status"] == "ok"]
    by_phase_trial: dict[tuple[str, int], dict[str, int | None]] = {}
    for record in successful:
        key = (record["trial_phase"], record["trial_number"])
        by_phase_trial.setdefault(key, {})[record["form"]] = record["measurements"]["result_checksum"]
    for key, checksums in by_phase_trial.items():
        if set(checksums) == set(E2_FORMS) and len(set(checksums.values())) != 1:
            raise RuntimeError(
                f"E2 semantic checksum mismatch at rho={rho}, Lambda={lambda_value}, trial={key}: {checksums}"
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
    if args.warmups < 0 or args.repetitions < 1 or args.timeout <= 0:
        raise SystemExit("invalid warmup, repetition, or timeout value")
    families = ("E1", "E2") if args.family == "both" else (args.family,)
    (OUTPUT_ROOT / "results/raw").mkdir(parents=True, exist_ok=True)
    (OUTPUT_ROOT / "results/plans").mkdir(parents=True, exist_ok=True)
    source_digest = source_hash()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary = {}

    for family in families:
        family_run_id = f"{family.lower()}-sqlite-{stamp}-{uuid.uuid4().hex[:8]}"
        raw_path = OUTPUT_ROOT / "results" / "raw" / f"{family_run_id}.jsonl"
        all_records: list[dict[str, Any]] = []
        with raw_path.open("x", encoding="utf-8") as raw_handle:
            if family == "E1":
                for rows in E1_ROWS:
                    rows_data, dataset_path, dataset_sha256 = prepare_dataset(rows, 1.0, "fixed_loop")
                    for bounds in E1_LOOPS:
                        print(f"START E1 R={rows} Lambda={amplification(bounds)}", flush=True)
                        all_records.extend(
                            run_configuration(
                                family=family, family_run_id=family_run_id, raw_handle=raw_handle,
                                raw_path=raw_path, rows_data=rows_data, dataset_path=dataset_path,
                                dataset_sha256=dataset_sha256, rows=rows, bounds=bounds, rho=1.0,
                                form=None, warmups=args.warmups, repetitions=args.repetitions,
                                timeout_seconds=args.timeout, source_digest=source_digest,
                            )
                        )
            else:
                for rho in E2_RHOS:
                    rows_data, dataset_path, dataset_sha256 = prepare_dataset(
                        E2_ROWS, rho, "relational_selectivity"
                    )
                    for bounds in E2_LOOPS:
                        pair_records = []
                        for form in E2_FORMS:
                            print(
                                f"START E2 R={E2_ROWS} rho={rho:g} "
                                f"Lambda={amplification(bounds)} form={form}",
                                flush=True,
                            )
                            pair_records.extend(
                                run_configuration(
                                    family=family, family_run_id=family_run_id, raw_handle=raw_handle,
                                    raw_path=raw_path, rows_data=rows_data, dataset_path=dataset_path,
                                    dataset_sha256=dataset_sha256, rows=E2_ROWS, bounds=bounds, rho=rho,
                                    form=form, warmups=args.warmups, repetitions=args.repetitions,
                                    timeout_seconds=args.timeout, source_digest=source_digest,
                                )
                            )
                        validate_pair(pair_records, rho, amplification(bounds))
                        all_records.extend(pair_records)
        summary[family] = {
            "run_id": family_run_id,
            "raw_path": str(raw_path),
            "records": len(all_records),
            "measurement_trials": sum(r["trial_phase"] == "measurement" for r in all_records),
            "non_ok": sum(r["status"] != "ok" for r in all_records),
        }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
