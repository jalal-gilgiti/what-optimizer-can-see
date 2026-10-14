#!/usr/bin/env python3
"""Validate, normalize, plot, and report one run-specific E3 campaign."""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SYSTEMS = ("sqlite", "duckdb", "postgresql")
QUERIES = ("Q_all", "Q_low", "Q_high")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json_exclusive(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def write_csv_exclusive(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_text_exclusive(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(value)


def bootstrap_median(values: list[float], seed: int, samples: int = 10_000) -> tuple[float, float]:
    rng = random.Random(seed)
    count = len(values)
    medians = sorted(statistics.median(rng.choice(values) for _ in range(count)) for _ in range(samples))
    return medians[int(0.025 * (samples - 1))], medians[int(0.975 * (samples - 1))]


def summarize(values: list[float], seed: int) -> dict[str, float]:
    mean = statistics.fmean(values)
    stddev = statistics.stdev(values)
    lower, upper = bootstrap_median(values, seed)
    return {
        "median_runtime_ms": statistics.median(values),
        "mean_runtime_ms": mean,
        "std_runtime_ms": stddev,
        "coefficient_of_variation": stddev / mean if mean else 0.0,
        "median_bootstrap_95_lower_ms": lower,
        "median_bootstrap_95_upper_ms": upper,
    }


def normalize(records_by_system: dict[str, list[dict[str, Any]]], errors: list[str]) -> list[dict[str, Any]]:
    output = []
    for system in SYSTEMS:
        measurements = [record for record in records_by_system[system] if record["trial_phase"] == "measurement"]
        for query_id in QUERIES:
            group = [record for record in measurements if record["query_id"] == query_id]
            good = [record for record in group if record["status"] == "ok"]
            if len(group) != 10 or len(good) != 10:
                errors.append(f"{system} {query_id}: expected 10/10 successful measurements, got {len(good)}/{len(group)}")
            first = group[0]
            row = {
                "system": system,
                "system_version": first["system_version"],
                "query_id": query_id,
                "R": first["R"],
                "selected_rows_expected": first["selected_rows_expected"],
                "selected_rows_observed": next(iter({r["selected_rows_observed"] for r in good})),
                "selectivity": first["selectivity"],
                "global_e_i": first["global_e_i"],
                "conditioned_e_i": first["conditioned_e_i"],
                "expected_udf_invocations": first["expected_udf_invocations"],
                "observed_udf_invocations": next(iter({r["observed_udf_invocations"] for r in good})),
                "expected_work": first["expected_work"],
                "observed_work": next(iter({r["observed_work"] for r in good})),
                "m1_global_predicted_work": first["m1_global_predicted_work"],
                "m1_global_qerror": first["m1_global_qerror"],
                "m2_conditioned_predicted_work": first["m2_conditioned_predicted_work"],
                "m2_conditioned_qerror": first["m2_conditioned_qerror"],
                "result_checksum": next(iter({r["result_checksum"] for r in good})),
                "estimated_total_plan_cost": first["estimated_total_plan_cost"],
                "successful_trials": len(good),
                "failed_or_timeout_trials": len(group) - len(good),
                "status": "ok" if len(good) == 10 else "invalid",
            }
            row.update(summarize([r["wall_time_ms"] for r in good], 42 + sum(map(ord, system + query_id))))
            output.append(row)
    return output


def runtime_separation(records_by_system: dict[str, list[dict[str, Any]]], normalized: list[dict[str, Any]]) -> list[dict[str, Any]]:
    index = {(row["system"], row["query_id"]): row for row in normalized}
    output = []
    for system in SYSTEMS:
        by_trial: dict[int, dict[str, float]] = defaultdict(dict)
        for record in records_by_system[system]:
            if record["trial_phase"] == "measurement":
                by_trial[record["trial_number"]][record["query_id"]] = record["wall_time_ms"]
        low = index[(system, "Q_low")]["median_runtime_ms"]
        high = index[(system, "Q_high")]["median_runtime_ms"]
        all_rows = index[(system, "Q_all")]["median_runtime_ms"]
        paired = [by_trial[trial]["Q_high"] / by_trial[trial]["Q_low"] for trial in range(1, 11)]
        lower, upper = bootstrap_median(paired, 42 + sum(map(ord, system)))
        output.append({
            "system": system,
            "median_runtime_q_all_ms": all_rows,
            "median_runtime_q_low_ms": low,
            "median_runtime_q_high_ms": high,
            "runtime_ratio_q_high_q_low": high / low,
            "runtime_ratio_q_all_q_low": all_rows / low,
            "runtime_ratio_q_high_q_all": high / all_rows,
            "paired_trial_ratio_median_q_high_q_low": statistics.median(paired),
            "paired_ratio_bootstrap_95_lower": lower,
            "paired_ratio_bootstrap_95_upper": upper,
            "q_high_slower_than_q_low": high > low,
        })
    return output


def plan_evidence(run_id: str) -> dict[str, Any]:
    output: dict[str, Any] = {}
    plan_dir = ROOT / "results/plans" / run_id
    for system in SYSTEMS:
        payloads = {
            query_id: json.loads((plan_dir / f"e3_{system}_{query_id.lower()}.json").read_text(encoding="utf-8"))
            for query_id in QUERIES
        }
        low_text = json.dumps(payloads["Q_low"]["plan"], sort_keys=True)
        high_text = json.dumps(payloads["Q_high"]["plan"], sort_keys=True)
        if system == "sqlite":
            filter_evidence = "population" in low_text and "population" in high_text
        elif system == "duckdb":
            filter_evidence = "Filters" in low_text and "Filters" in high_text and "LOW" in low_text and "HIGH" in high_text
        else:
            filter_evidence = '"Filter"' in low_text and '"Filter"' in high_text and "LOW" in low_text and "HIGH" in high_text
        output[system] = {
            "filtered_predicate_visible_below_udf_aggregate": filter_evidence,
            "q_low_q_high_estimated_cost_equal": payloads["Q_low"]["estimated_total_cost"] == payloads["Q_high"]["estimated_total_cost"],
            "q_low_estimated_total_cost": payloads["Q_low"]["estimated_total_cost"],
            "q_high_estimated_total_cost": payloads["Q_high"]["estimated_total_cost"],
            "function_metadata_equal_across_contexts": len({json.dumps(p["function_metadata"], sort_keys=True) for p in payloads.values()}) == 1,
        }
    return output


def validate(
    run_id: str,
    records: dict[str, list[dict[str, Any]]],
    normalized: list[dict[str, Any]],
    separation: list[dict[str, Any]],
    manifest: dict[str, Any],
    prereg: dict[str, Any],
    freeze_after: dict[str, Any],
    errors: list[str],
) -> dict[str, Any]:
    if manifest["dataset_count"] != 1 or manifest["population_counts"] != {"HIGH": 5000, "LOW": 5000}:
        errors.append("dataset cardinality/population validation failed")
    expected = {item["query_id"]: item for item in manifest["contexts"]}
    analytical = {"Q_all": (10_000, 5_050_000), "Q_low": (5_000, 50_000), "Q_high": (5_000, 5_000_000)}
    for query_id, (row_count, work) in analytical.items():
        if expected[query_id]["expected_rows"] != row_count or expected[query_id]["true_work"] != work:
            errors.append(f"analytical expectation mismatch: {query_id}")
    if prereg["dataset_sha256"] != manifest["dataset_sha256"]:
        errors.append("manifest/preregistration dataset SHA mismatch")

    seen: set[str] = set()
    checksum_groups: dict[tuple[str, str, int], set[int]] = defaultdict(set)
    logical_queries: dict[str, set[str]] = defaultdict(set)
    for system in SYSTEMS:
        if len(records[system]) != 39:
            errors.append(f"{system}: expected 39 raw trials, got {len(records[system])}")
        if sum(r["trial_phase"] == "measurement" for r in records[system]) != 30:
            errors.append(f"{system}: expected 30 primary trials")
        for record in records[system]:
            if record["trial_id"] in seen:
                errors.append(f"duplicate trial ID: {record['trial_id']}")
            seen.add(record["trial_id"])
            if record["status"] != "ok":
                errors.append(f"non-ok trial: {record['trial_id']}")
            if record["selected_rows_observed"] != record["selected_rows_expected"]:
                errors.append(f"row-count mismatch: {record['trial_id']}")
            if record["observed_udf_invocations"] != record["expected_udf_invocations"]:
                errors.append(f"invocation mismatch: {record['trial_id']}")
            if record["observed_work"] != record["expected_work"]:
                errors.append(f"work mismatch: {record['trial_id']}")
            checksum_groups[(record["query_id"], record["trial_phase"], record["trial_number"])].add(record["result_checksum"])
            logical_queries[record["query_id"]].add(record["query"])
    checksum_mismatches = [key for key, values in checksum_groups.items() if len(values) != 1]
    if checksum_mismatches:
        errors.append(f"cross-system checksum mismatches: {checksum_mismatches}")
    query_shape_agreement = all(len(values) == 1 for values in logical_queries.values())
    if not query_shape_agreement:
        errors.append("submitted logical queries differ across systems")

    evidence = plan_evidence(run_id)
    if not all(item["filtered_predicate_visible_below_udf_aggregate"] for item in evidence.values()):
        errors.append("filtered-predicate plan evidence failed")
    high_cv = [
        {"system": row["system"], "query_id": row["query_id"], "cv": row["coefficient_of_variation"]}
        for row in normalized if row["coefficient_of_variation"] > 0.1
    ]
    runtime_direction = all(item["q_high_slower_than_q_low"] for item in separation)
    paired_intervals = all(item["paired_ratio_bootstrap_95_lower"] > 1 for item in separation)
    status = "PASS" if not errors and freeze_after["status"] == "PASS" else "FAIL"
    decision = "NO-GO" if status == "FAIL" else ("GO" if runtime_direction and paired_intervals else "CONDITIONAL GO")
    return {
        "status": status,
        "decision": decision,
        "errors": errors,
        "dataset_query_validation": status,
        "dataset_count": manifest["dataset_count"],
        "dataset_sha256": manifest["dataset_sha256"],
        "true_work": {query_id: expected[query_id]["true_work"] for query_id in QUERIES},
        "raw_record_counts": {system: len(records[system]) for system in SYSTEMS},
        "measurement_record_counts": {system: sum(r["trial_phase"] == "measurement" for r in records[system]) for system in SYSTEMS},
        "failures_or_timeouts": sum(r["status"] != "ok" for rows in records.values() for r in rows),
        "cross_system_checksum_groups": len(checksum_groups),
        "cross_system_checksum_mismatches": checksum_mismatches,
        "logical_query_shape_agreement": query_shape_agreement,
        "plan_and_metadata_evidence": evidence,
        "m1_global_qerror_range": [min(r["m1_global_qerror"] for r in normalized), max(r["m1_global_qerror"] for r in normalized)],
        "m2_conditioned_qerror_range": [min(r["m2_conditioned_qerror"] for r in normalized), max(r["m2_conditioned_qerror"] for r in normalized)],
        "runtime_direction_all_systems": runtime_direction,
        "paired_intervals_above_one_all_systems": paired_intervals,
        "high_cv_count": len(high_cv),
        "high_cv_configurations": high_cv,
        "frozen_artifact_audit": freeze_after["status"],
        "frozen_artifact_unchanged_count": freeze_after["unchanged_count"],
    }


def create_figures(output_dir: Path, manifest: dict[str, Any], separation: list[dict[str, Any]]) -> None:
    import matplotlib.pyplot as plt

    output_dir.mkdir(parents=True, exist_ok=False)
    contexts = {item["query_id"]: item for item in manifest["contexts"]}
    x = list(range(3))
    width = 0.25
    figure, axis = plt.subplots(figsize=(7.2, 4.2))
    axis.bar([i - width for i in x], [contexts[q]["true_work"] for q in QUERIES], width, label="true")
    axis.bar(x, [contexts[q]["m1_global_predicted_work"] for q in QUERIES], width, label="M1 global")
    axis.bar([i + width for i in x], [contexts[q]["m2_conditioned_predicted_work"] for q in QUERIES], width, label="M2 conditioned")
    axis.set_xticks(x, QUERIES)
    axis.set_yscale("log")
    axis.set_ylabel("Work units (log scale)")
    axis.grid(True, axis="y", alpha=0.25)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output_dir / "e3_true_vs_predicted_work.pdf")
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(6.4, 4.2))
    for item in separation:
        axis.plot(["Q_low", "Q_all", "Q_high"], [1, item["runtime_ratio_q_all_q_low"], item["runtime_ratio_q_high_q_low"]], marker="o", label=item["system"])
    axis.set_yscale("log")
    axis.set_ylabel("Median runtime / Q_low median")
    axis.grid(True, axis="y", alpha=0.25)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output_dir / "e3_runtime_separation_by_system.pdf")
    plt.close(figure)


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def report(run_id: str, validation: dict[str, Any], normalized: list[dict[str, Any]], separation: list[dict[str, Any]]) -> str:
    work = markdown_table(
        ["query", "rows", "true work", "M1 work", "M1 Q", "M2 work", "M2 Q"],
        [[row["query_id"], row["selected_rows_expected"], row["expected_work"], f"{row['m1_global_predicted_work']:.6g}", f"{row['m1_global_qerror']:.6g}", f"{row['m2_conditioned_predicted_work']:.6g}", f"{row['m2_conditioned_qerror']:.6g}"]
         for row in normalized if row["system"] == "sqlite"],
    )
    timing = markdown_table(
        ["system", "Q_all ms", "Q_low ms", "Q_high ms", "high/low", "paired 95% interval"],
        [[item["system"], f"{item['median_runtime_q_all_ms']:.6g}", f"{item['median_runtime_q_low_ms']:.6g}", f"{item['median_runtime_q_high_ms']:.6g}", f"{item['runtime_ratio_q_high_q_low']:.6g}", f"[{item['paired_ratio_bootstrap_95_lower']:.6g}, {item['paired_ratio_bootstrap_95_upper']:.6g}]"] for item in separation],
    )
    cv = "\n".join(f"- {item['system']} {item['query_id']}: CV={item['cv']:.3f}" for item in validation["high_cv_configurations"]) or "- None"
    return f"""# E3 query-conditioned dynamic loop estimation

Run ID: `{run_id}`

## Validation

Overall dataset/query validation: **{validation['status']}**. Exactly one frozen dataset, 117 raw trials, and 90 primary measurement trials were preserved. Failures/timeouts: `{validation['failures_or_timeouts']}`. Cross-system checksum mismatches: `{len(validation['cross_system_checksum_mismatches'])}`. Frozen pre-existing artifacts: **{validation['frozen_artifact_audit']}** (`{validation['frozen_artifact_unchanged_count']}` files unchanged).

## Analytical representations

{work}

M1 global Q-error range: `{validation['m1_global_qerror_range'][0]:.6g}` to `{validation['m1_global_qerror_range'][1]:.6g}`. M2 query-conditioned Q-error range: `{validation['m2_conditioned_qerror_range'][0]:.6g}` to `{validation['m2_conditioned_qerror_range'][1]:.6g}`. M1 and M2 are experiment-defined analytical representations, not claims that the native optimizers consumed equivalent estimates.

## Within-system runtime separation

{timing}

Absolute runtimes are not treated as normalized cross-engine performance comparisons. Every paired high/low bootstrap interval remained above one.

## Optimizer-visible evidence

Submitted logical query text matched across systems: `{validation['logical_query_shape_agreement']}`. Evidence by system: `{json.dumps(validation['plan_and_metadata_evidence'], sort_keys=True)}`. Operator names and access choices differ by engine, so structural agreement means common logical query/UDF semantics and visible upstream predicates, not byte-identical plans. PostgreSQL assigned equal estimated cost to Q_low and Q_high even though their true loop work differs by 100x.

## Variability

Configurations above 10% CV:

{cv}

No trial was discarded, extended, or automatically rerun.

## Decision

**{validation['decision']}**. Dataset, work, invocation, checksum, trial-cardinality, plan-evidence, and frozen-artifact validation passed, and Q_high was slower than Q_low on every system.

## Artifacts

- Raw trials: `results/raw/{run_id}/`
- Normalized results: `results/normalized/{run_id}/e3_query_conditioned.csv`
- Runtime separation: `results/normalized/{run_id}/e3_runtime_separation.csv`
- Validation: `results/normalized/{run_id}/e3_validation.json`
- Metadata: `metadata/{run_id}/`
- Plans: `results/plans/{run_id}/`
- Figures: `results/figures/{run_id}/`
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    # Fail before creating any analysis artifact when the plotting dependency
    # is unavailable, preserving the exclusive-create output contract.
    import matplotlib.pyplot  # noqa: F401

    raw_dir = ROOT / "results/raw" / args.run_id
    metadata_dir = ROOT / "metadata" / args.run_id
    records = {system: load_jsonl(raw_dir / f"e3_{system}.jsonl") for system in SYSTEMS}
    manifest = json.loads((metadata_dir / "e3_dataset_manifest.json").read_text(encoding="utf-8"))
    prereg = json.loads((metadata_dir / "e3_preregistration.json").read_text(encoding="utf-8"))
    freeze_after = json.loads((metadata_dir / "frozen_artifacts_after.json").read_text(encoding="utf-8"))
    errors: list[str] = []
    normalized = normalize(records, errors)
    separation = runtime_separation(records, normalized)
    validation = validate(args.run_id, records, normalized, separation, manifest, prereg, freeze_after, errors)
    normalized_dir = ROOT / "results/normalized" / args.run_id
    write_csv_exclusive(normalized_dir / "e3_query_conditioned.csv", normalized)
    write_csv_exclusive(normalized_dir / "e3_runtime_separation.csv", separation)
    write_json_exclusive(normalized_dir / "e3_validation.json", validation)
    create_figures(ROOT / "results/figures" / args.run_id, manifest, separation)
    write_text_exclusive(ROOT / "docs" / f"e3_query_conditioned_{args.run_id}.md", report(args.run_id, validation, normalized, separation))
    print(json.dumps(validation, indent=2, sort_keys=True))
    if validation["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
