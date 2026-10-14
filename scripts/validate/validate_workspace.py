#!/usr/bin/env python3
"""Validate repository structure, metadata, and every preserved raw trial."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REQUIRED_PATHS = (
    "README.md", "Makefile", ".gitignore",
    "docs/environment.md", "docs/methodology.md", "docs/systems.md",
    "docs/workload.md", "docs/reproduction.md", "docs/representation_matrix.md",
    "docs/bootstrap_report.md", "docs/e1_e2_report.md", "metadata/environment.json", "metadata/systems.json",
    "metadata/experiment_manifest.json", "configs/experiments/core_experiments.json",
    "results/normalized/e1_amplification.csv", "results/normalized/e2_selectivity_amplification.csv",
    "results/normalized/e1_e2_validation.json",
    "results/figures/e1_runtime_vs_amplification.pdf", "results/figures/e1_runtime_vs_work.pdf",
    "results/figures/e2_runtime_ratio_vs_lambda.pdf", "results/figures/e2_regret_vs_lambda.pdf",
    "docs/duckdb_environment.md", "docs/e1_e2_duckdb_report.md",
    "metadata/duckdb_environment.json", "configs/systems/duckdb-requirements.txt",
    "results/normalized/e1_duckdb.csv", "results/normalized/e2_duckdb.csv",
    "results/normalized/e1_e2_duckdb_validation.json",
    "results/normalized/e1_e2_sqlite_duckdb_comparison.csv",
    "results/figures/e1_duckdb_runtime_vs_amplification.pdf",
    "results/figures/e1_duckdb_runtime_vs_work.pdf",
    "results/figures/e2_duckdb_runtime_ratio_vs_lambda.pdf",
    "results/figures/e2_duckdb_regret_vs_lambda.pdf",
)
VALID_SYSTEM_STATUSES = {
    "AVAILABLE", "INSTALLABLE", "ARTIFACT_ONLY", "NOT_AVAILABLE", "NOT_YET_VERIFIED"
}
VALID_NEXT_STAGE_STATUSES = {
    "READY", "EASY_LOCAL_SETUP", "CONTAINER_SETUP", "REQUIRES_ARTIFACT_WORK", "BLOCKED"
}


def load_json(path: Path, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"invalid JSON {path.relative_to(ROOT)}: {exc}")
        return None


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    for relative in REQUIRED_PATHS:
        if not (ROOT / relative).exists():
            errors.append(f"missing required path: {relative}")

    for relative in ("metadata/environment.json", "metadata/systems.json", "metadata/experiment_manifest.json", "configs/experiments/core_experiments.json"):
        path = ROOT / relative
        if path.exists():
            load_json(path, errors)

    systems_path = ROOT / "metadata" / "systems.json"
    if systems_path.exists():
        systems = load_json(systems_path, errors) or {}
        for item in systems.get("systems", []):
            if item.get("status") not in VALID_SYSTEM_STATUSES:
                errors.append(f"invalid system status for {item.get('system')}: {item.get('status')}")
            if item.get("next_stage_status") not in VALID_NEXT_STAGE_STATUSES:
                errors.append(
                    f"invalid next-stage status for {item.get('system')}: {item.get('next_stage_status')}"
                )
        for item in systems.get("external_artifacts", []):
            if item.get("next_stage_status") not in VALID_NEXT_STAGE_STATUSES:
                errors.append(
                    f"invalid next-stage status for {item.get('artifact')}: {item.get('next_stage_status')}"
                )

    trial_ids: set[str] = set()
    by_configuration: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    raw_paths = sorted((ROOT / "results" / "raw").glob("*.jsonl"))
    if not raw_paths:
        warnings.append("no raw result files found; run `make smoke`")
    for path in raw_paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"{path.name}:{line_number}: invalid JSON: {exc}")
                continue
            trial_id = record.get("trial_id")
            if not trial_id:
                errors.append(f"{path.name}:{line_number}: missing trial_id")
            elif trial_id in trial_ids:
                errors.append(f"duplicate trial_id: {trial_id}")
            trial_ids.add(trial_id)
            configuration_id = record.get("configuration_id", record.get("experiment_id", "missing-configuration-id"))
            by_configuration[(record.get("run_id", "missing-run-id"), configuration_id)].append(record)
            if record.get("status") != "ok":
                warnings.append(f"non-ok trial {trial_id}: {record.get('status')}")
            measurements = record.get("measurements", {})
            expected = (
                round(record.get("dataset_scale", 0) * record["rho"])
                if record.get("experiment_family") == "E2" else record.get("dataset_scale")
            )
            actual = measurements.get("rows_after_filter")
            if expected is not None and actual is not None and expected != actual:
                errors.append(f"unexpected row count for {trial_id}: expected {expected}, got {actual}")
            if record.get("status") == "ok" and "expected_loop_iterations" in measurements:
                if measurements["expected_loop_iterations"] != measurements.get("observed_loop_iterations"):
                    errors.append(f"effective-loop mismatch for {trial_id}")
                if measurements.get("expected_udf_invocations") != measurements.get("udf_invocations"):
                    errors.append(f"UDF-invocation mismatch for {trial_id}")

    for (run_id, configuration_id), records in by_configuration.items():
        hashes = {record.get("configuration_hash") for record in records}
        versions = {(record.get("system"), record.get("system_version")) for record in records}
        if len(hashes) != 1:
            errors.append(f"configuration {run_id}/{configuration_id} has mismatched configuration hashes")
        if len(versions) != 1:
            errors.append(f"configuration {run_id}/{configuration_id} has mismatched system versions")
        expected_measurements = records[0].get("repetition_count")
        actual_measurements = sum(record.get("trial_phase") == "measurement" for record in records)
        if expected_measurements != actual_measurements:
            errors.append(
                f"configuration {run_id}/{configuration_id} missing trials: expected {expected_measurements}, got {actual_measurements}"
            )

    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}")
    print(f"validated {len(raw_paths)} raw file(s), {len(trial_ids)} trial(s), {len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
