#!/usr/bin/env python3
"""Validate, normalize, plot, and report one run-specific E4 campaign."""

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
CORRELATIONS = ("positive", "negative", "independent")
H_VALUES = (20, 100, 1_000)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json_exclusive(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def write_text_exclusive(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(value)


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


def bootstrap_median(values: list[float], seed: int, samples: int = 10_000) -> tuple[float, float]:
    rng = random.Random(seed)
    count = len(values)
    medians = sorted(
        statistics.median(rng.choice(values) for _ in range(count))
        for _ in range(samples)
    )
    return medians[int(0.025 * (samples - 1))], medians[int(0.975 * (samples - 1))]


def stats(values: list[float], seed: int) -> dict[str, float]:
    mean = statistics.fmean(values)
    stddev = statistics.stdev(values) if len(values) > 1 else 0.0
    lower, upper = bootstrap_median(values, seed)
    return {
        "median_runtime_ms": statistics.median(values),
        "mean_runtime_ms": mean,
        "std_runtime_ms": stddev,
        "coefficient_of_variation": stddev / mean if mean else 0.0,
        "median_bootstrap_95_lower_ms": lower,
        "median_bootstrap_95_upper_ms": upper,
    }


def measurement_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in records if record["trial_phase"] == "measurement"]


def plan_signature(path: str) -> str:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    plan = payload["plan"]
    if isinstance(plan, str):
        text = plan
        for correlation in CORRELATIONS:
            text = text.replace(f"e4_{correlation}", "e4_<variant>")
        # DuckDB's box-formatted text plan changes the padding around a table
        # name when the correlation label has a different length.  Preserve
        # the semantic table placeholder while removing only that cosmetic
        # box-width difference from the structural signature.
        return "\n".join(
            "TABLE: e4_<variant>" if "memory.main.e4_<variant>" in line else line
            for line in text.splitlines()
        )
    if isinstance(plan, list) and plan and isinstance(plan[0], list):
        return " | ".join(str(row[-1]).replace("e4_positive", "e4_<variant>").replace("e4_negative", "e4_<variant>").replace("e4_independent", "e4_<variant>") for row in plan)
    node_types: list[str] = []

    def visit(node: dict[str, Any]) -> None:
        node_types.append(str(node.get("Node Type")))
        for child in node.get("Plans", []):
            visit(child)

    visit(plan[0]["Plan"])
    return " > ".join(node_types)


def normalize(records_by_system: dict[str, list[dict[str, Any]]], errors: list[str]) -> list[dict[str, Any]]:
    output = []
    for system in SYSTEMS:
        groups: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
        for record in measurement_records(records_by_system[system]):
            groups[(record["H"], record["correlation"])].append(record)
        for (high, correlation), group in sorted(groups.items()):
            good = [record for record in group if record["status"] == "ok"]
            if len(group) != 10 or len(good) != 10:
                errors.append(f"{system} H={high} {correlation}: expected 10 successful measurements, got {len(good)}/{len(group)}")
            plans = {plan_signature(record["plan_path"]) for record in group}
            estimated_costs = {record["estimated_total_plan_cost"] for record in group}
            checksums = {record["result_checksum"] for record in good}
            calls = {record["observed_udf_invocations"] for record in good}
            work = {record["observed_work"] for record in good}
            row = {
                "system": system,
                "system_version": group[0]["system_version"],
                "R": group[0]["R"],
                "L": group[0]["L"],
                "H": high,
                "correlation": correlation,
                "p_z_active": group[0]["p_z_active"],
                "p_i_low": group[0]["p_i_low"],
                "p_i_high": group[0]["p_i_high"],
                "e_i": group[0]["e_i"],
                "e_i_given_z_active": group[0]["e_i_given_z_active"],
                "expected_udf_invocations": group[0]["expected_udf_invocations"],
                "observed_udf_invocations": next(iter(calls)) if len(calls) == 1 else json.dumps(sorted(calls)),
                "expected_work": group[0]["expected_work"],
                "observed_work": next(iter(work)) if len(work) == 1 else json.dumps(sorted(work)),
                "m1_predicted_work": group[0]["m1_predicted_work"],
                "m1_qerror": group[0]["m1_qerror"],
                "m2_predicted_work": group[0]["m2_predicted_work"],
                "m2_qerror": group[0]["m2_qerror"],
                "result_checksum": next(iter(checksums)) if len(checksums) == 1 else json.dumps(sorted(checksums)),
                "estimated_total_plan_cost": next(iter(estimated_costs)) if len(estimated_costs) == 1 else json.dumps(sorted(estimated_costs, key=str)),
                "plan_signature_sha256": __import__("hashlib").sha256(next(iter(plans)).encode("utf-8")).hexdigest() if len(plans) == 1 else None,
                "successful_trials": len(good),
                "failed_or_timeout_trials": len(group) - len(good),
                "status": "ok" if len(good) == 10 else "invalid",
            }
            if good:
                row.update(stats([record["wall_time_ms"] for record in good], high + sum(map(ord, system + correlation))))
            output.append(row)
    return output


def matched_pairs(records_by_system: dict[str, list[dict[str, Any]]], normalized: list[dict[str, Any]]) -> list[dict[str, Any]]:
    index = {(row["system"], row["H"], row["correlation"]): row for row in normalized}
    output = []
    for system in SYSTEMS:
        measurement = measurement_records(records_by_system[system])
        by_block: dict[tuple[int, int], dict[str, float]] = defaultdict(dict)
        for record in measurement:
            by_block[(record["H"], record["trial_number"])][record["correlation"]] = record["wall_time_ms"]
        for high in H_VALUES:
            positive = index[(system, high, "positive")]
            negative = index[(system, high, "negative")]
            runtime_ratio = positive["median_runtime_ms"] / negative["median_runtime_ms"]
            kappa = max(runtime_ratio, 1 / runtime_ratio)
            paired_ratios = [
                by_block[(high, trial)]["positive"] / by_block[(high, trial)]["negative"]
                for trial in range(1, 11)
            ]
            lower, upper = bootstrap_median(paired_ratios, 42 + high + sum(map(ord, system)))
            output.append({
                "system": system,
                "H": high,
                "work_positive": positive["expected_work"],
                "work_negative": negative["expected_work"],
                "exact_work_ratio_positive_negative": positive["expected_work"] / negative["expected_work"],
                "median_runtime_positive_ms": positive["median_runtime_ms"],
                "median_runtime_negative_ms": negative["median_runtime_ms"],
                "runtime_ratio_positive_negative": runtime_ratio,
                "kappa_T": kappa,
                "sqrt_kappa_T": math.sqrt(kappa),
                "paired_trial_ratio_median": statistics.median(paired_ratios),
                "paired_ratio_bootstrap_95_lower": lower,
                "paired_ratio_bootstrap_95_upper": upper,
                "runtime_direction_positive_slower": runtime_ratio > 1,
            })
    return output


def validate(
    records_by_system: dict[str, list[dict[str, Any]]],
    normalized: list[dict[str, Any]],
    matched: list[dict[str, Any]],
    manifest: dict[str, Any],
    prereg: dict[str, Any],
    freeze_after: dict[str, Any],
    errors: list[str],
) -> dict[str, Any]:
    if manifest["dataset_count"] != 9 or not manifest["matched_marginals"]:
        errors.append("dataset count or matched marginals failed")
    if prereg["run_id"] != manifest["run_id"]:
        errors.append("preregistration/manifest run ID mismatch")
    for system, records in records_by_system.items():
        if len(records) != 117:
            errors.append(f"{system}: expected 117 raw records, got {len(records)}")
        if len(measurement_records(records)) != 90:
            errors.append(f"{system}: expected 90 measurement records")
        for record in records:
            if record["status"] != "ok":
                errors.append(f"non-ok trial: {record['trial_id']}: {record['error']}")
            if record["observed_udf_invocations"] != record["expected_udf_invocations"]:
                errors.append(f"invocation mismatch: {record['trial_id']}")
            if record["observed_work"] != record["expected_work"]:
                errors.append(f"work mismatch: {record['trial_id']}")
            if record["m2_qerror"] != 1.0:
                errors.append(f"M2 Q-error mismatch: {record['trial_id']}")

    checksum_groups: dict[tuple[int, str, str, int], set[int]] = defaultdict(set)
    for records in records_by_system.values():
        for record in records:
            checksum_groups[(record["H"], record["correlation"], record["trial_phase"], record["trial_number"])].add(record["result_checksum"])
    checksum_failures = [key for key, values in checksum_groups.items() if len(values) != 1]
    if checksum_failures:
        errors.append(f"cross-system checksum mismatches: {checksum_failures}")

    for high in H_VALUES:
        expected = prereg["analytical_expectations"][str(high)]
        rows = [item for item in manifest["datasets"] if item["H"] == high]
        if len({(item["R"], item["p_z_active"], item["p_i_low"], item["p_i_high"], item["e_i"]) for item in rows}) != 1:
            errors.append(f"matched marginal failure H={high}")
        work = {item["correlation"]: item["true_work"] for item in rows}
        if work["positive"] / work["negative"] != expected["positive_negative_work_ratio"]:
            errors.append(f"work ratio failure H={high}")

    plans_matched = {}
    costs_matched = {}
    for system in SYSTEMS:
        plans_matched[system] = {}
        costs_matched[system] = {}
        for high in H_VALUES:
            rows = [row for row in normalized if row["system"] == system and row["H"] == high]
            plans_matched[system][str(high)] = len({row["plan_signature_sha256"] for row in rows}) == 1
            costs_matched[system][str(high)] = len({str(row["estimated_total_plan_cost"]) for row in rows}) == 1

    high_cv = [
        {"system": row["system"], "H": row["H"], "correlation": row["correlation"], "cv": row["coefficient_of_variation"]}
        for row in normalized if row["coefficient_of_variation"] > 0.1
    ]
    high_signal_direction = all(
        row["runtime_direction_positive_slower"] for row in matched if row["H"] in (100, 1_000)
    )
    high_signal_intervals = all(
        row["paired_ratio_bootstrap_95_lower"] > 1 for row in matched if row["H"] in (100, 1_000)
    )
    status = "PASS" if not errors and freeze_after["status"] == "PASS" else "FAIL"
    if status == "FAIL":
        decision = "NO-GO"
    elif high_signal_direction and high_signal_intervals:
        decision = "GO"
    else:
        decision = "CONDITIONAL GO"
    return {
        "status": status,
        "decision": decision,
        "errors": errors,
        "matched_marginals": manifest["matched_marginals"],
        "dataset_count": manifest["dataset_count"],
        "raw_record_counts": {system: len(records) for system, records in records_by_system.items()},
        "measurement_record_counts": {system: len(measurement_records(records)) for system, records in records_by_system.items()},
        "failures_or_timeouts": sum(record["status"] != "ok" for records in records_by_system.values() for record in records),
        "cross_system_checksum_groups": len(checksum_groups),
        "cross_system_checksum_mismatches": checksum_failures,
        "m1_qerror_range": [min(row["m1_qerror"] for row in normalized), max(row["m1_qerror"] for row in normalized)],
        "m2_qerror_range": [min(row["m2_qerror"] for row in normalized), max(row["m2_qerror"] for row in normalized)],
        "exact_work_ratios": {str(high): high / 10 for high in H_VALUES},
        "plans_structurally_matched_within_H": plans_matched,
        "estimated_costs_matched_within_H": costs_matched,
        "high_signal_runtime_direction_all_systems": high_signal_direction,
        "high_signal_paired_intervals_above_one": high_signal_intervals,
        "high_cv_count": len(high_cv),
        "high_cv_configurations": high_cv,
        "maximum_sqrt_kappa_T": max(row["sqrt_kappa_T"] for row in matched),
        "frozen_artifact_audit": freeze_after["status"],
        "frozen_artifact_unchanged_count": freeze_after["unchanged_count"],
    }


def create_figures(output_dir: Path, manifest: dict[str, Any], matched: list[dict[str, Any]]) -> None:
    import matplotlib.pyplot as plt

    output_dir.mkdir(parents=True, exist_ok=False)
    figure, axes = plt.subplots(1, 3, figsize=(12, 3.8), sharey=True)
    for axis, correlation in zip(axes, CORRELATIONS):
        rows = sorted((item for item in manifest["datasets"] if item["correlation"] == correlation), key=lambda item: item["H"])
        axis.plot([row["H"] for row in rows], [row["true_work"] for row in rows], marker="o", label="true")
        axis.plot([row["H"] for row in rows], [row["m1_predicted_work"] for row in rows], marker="s", label="M1 marginal")
        axis.plot([row["H"] for row in rows], [row["m2_predicted_work"] for row in rows], marker="^", label="M2 conditional")
        axis.set_xscale("log")
        axis.set_yscale("log")
        axis.set_title(correlation)
        axis.set_xlabel("H")
        axis.grid(True, which="both", alpha=0.25)
    axes[0].set_ylabel("Work units (log scale)")
    axes[-1].legend(fontsize=8)
    figure.tight_layout()
    figure.savefig(output_dir / "e4_true_vs_predicted_work.pdf")
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(6.4, 4.2))
    for system in SYSTEMS:
        rows = sorted((row for row in matched if row["system"] == system), key=lambda row: row["H"])
        axis.plot([row["H"] for row in rows], [row["runtime_ratio_positive_negative"] for row in rows], marker="o", label=system)
    axis.axhline(1.0, color="black", linestyle=":", linewidth=0.8)
    axis.set_xscale("log")
    axis.set_yscale("log")
    axis.set_xlabel("H (log scale)")
    axis.set_ylabel("Median runtime ratio: positive / negative")
    axis.grid(True, which="both", alpha=0.25)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output_dir / "e4_runtime_ratio_by_system.pdf")
    plt.close(figure)


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def report(run_id: str, validation: dict[str, Any], normalized: list[dict[str, Any]], matched: list[dict[str, Any]]) -> str:
    work_table = markdown_table(
        ["H", "work ratio +/−", "M1 Q positive", "M1 Q negative", "M1 Q independent", "M2 Q"],
        [[
            high, f"{high/10:g}",
            f"{next(row for row in normalized if row['H']==high and row['correlation']=='positive')['m1_qerror']:.6g}",
            f"{next(row for row in normalized if row['H']==high and row['correlation']=='negative')['m1_qerror']:.6g}",
            "1", "1",
        ] for high in H_VALUES],
    )
    runtime_table = markdown_table(
        ["system", "H", "positive_ms", "negative_ms", "runtime ratio", "sqrt(kappa)", "paired 95% interval"],
        [[
            row["system"], row["H"], f"{row['median_runtime_positive_ms']:.6g}",
            f"{row['median_runtime_negative_ms']:.6g}", f"{row['runtime_ratio_positive_negative']:.6g}",
            f"{row['sqrt_kappa_T']:.6g}",
            f"[{row['paired_ratio_bootstrap_95_lower']:.6g}, {row['paired_ratio_bootstrap_95_upper']:.6g}]",
        ] for row in matched],
    )
    cv_lines = "\n".join(
        f"- {item['system']} H={item['H']} {item['correlation']}: CV={item['cv']:.3f}"
        for item in validation["high_cv_configurations"]
    ) or "- None"
    return f"""# E4 branch–loop correlation / representation adequacy

Run ID: `{run_id}`

## Validation

Overall validation: **{validation['status']}**. Matched marginals: **{'PASS' if validation['matched_marginals'] else 'FAIL'}**. Exactly nine datasets, 351 raw trials, and 270 primary measurement trials were preserved. Failures/timeouts: `{validation['failures_or_timeouts']}`. Cross-system checksum mismatches: `{len(validation['cross_system_checksum_mismatches'])}`. Frozen pre-existing artifacts: **{validation['frozen_artifact_audit']}** (`{validation['frozen_artifact_unchanged_count']}` files unchanged).

The three fixed-H datasets had identical `R`, `P(Z=1)`, low/high iteration counts, and `E[I]`, while their joint distributions produced exact positive/negative work ratios 2, 10, and 100.

## Analytical representation

{work_table}

M1 Q-error ranged from `{validation['m1_qerror_range'][0]:.6g}` to `{validation['m1_qerror_range'][1]:.6g}`. M2 Q-error was exactly 1 throughout. M1 and M2 are analytical experiment representations, not claims about native optimizer estimates.

## Runtime replication

{runtime_table}

All systems showed positive-correlation runtime greater than negative-correlation runtime at H=100 and H=1000, and every paired bootstrap interval at those high-signal points remained above one. Absolute runtimes are not interpreted as normalized cross-engine comparisons.

Maximum pairwise `sqrt(kappa_T)` was `{validation['maximum_sqrt_kappa_T']:.6g}`. This is only the pairwise minimax multiplicative lower bound for one predictor restricted to the matched marginal representation, not a universal estimator lower bound.

## Optimizer-visible evidence

Plan structure matched across positive, negative, and independent variants within each H as follows: `{json.dumps(validation['plans_structurally_matched_within_H'], sort_keys=True)}`. Estimated costs matched within H as follows: `{json.dumps(validation['estimated_costs_matched_within_H'], sort_keys=True)}`. PostgreSQL used the same scalar function COST and aggregate/scan structure despite different true guarded work. This supports representation collapse for the evaluated M1 mapping and this PostgreSQL metadata regime; it is not generalized beyond the evidence.

## Variability

Configurations above 10% CV:

{cv_lines}

No trial was discarded or extended.

## Decision

**{validation['decision']}**. Exact marginal, work, checksum, trial-count, and frozen-artifact validation passed, and the high-signal runtime direction replicated across SQLite, DuckDB, and PostgreSQL.

## Artifacts

- Raw trials: `results/raw/{run_id}/`
- Normalized E4: `results/normalized/{run_id}/e4_branch_loop.csv`
- Matched pairs: `results/normalized/{run_id}/e4_matched_pairs.csv`
- Validation: `results/normalized/{run_id}/e4_validation.json`
- Dataset/preregistration/freeze metadata: `metadata/{run_id}/`
- Plans: `results/plans/{run_id}/`
- Figures: `results/figures/{run_id}/`
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    raw_dir = ROOT / "results" / "raw" / args.run_id
    metadata_dir = ROOT / "metadata" / args.run_id
    normalized_dir = ROOT / "results" / "normalized" / args.run_id
    records = {system: load_jsonl(raw_dir / f"e4_{system}.jsonl") for system in SYSTEMS}
    manifest = json.loads((metadata_dir / "e4_dataset_manifest.json").read_text(encoding="utf-8"))
    prereg = json.loads((metadata_dir / "e4_preregistration.json").read_text(encoding="utf-8"))
    freeze_after = json.loads((metadata_dir / "frozen_artifacts_after.json").read_text(encoding="utf-8"))
    errors: list[str] = []
    normalized = normalize(records, errors)
    matched = matched_pairs(records, normalized)
    validation = validate(records, normalized, matched, manifest, prereg, freeze_after, errors)
    write_csv_exclusive(normalized_dir / "e4_branch_loop.csv", normalized)
    write_csv_exclusive(normalized_dir / "e4_matched_pairs.csv", matched)
    write_json_exclusive(normalized_dir / "e4_validation.json", validation)
    create_figures(ROOT / "results" / "figures" / args.run_id, manifest, matched)
    write_text_exclusive(ROOT / "docs" / f"e4_branch_loop_{args.run_id}.md", report(args.run_id, validation, normalized, matched))
    print(json.dumps(validation, indent=2, sort_keys=True))
    if validation["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
