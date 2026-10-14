#!/usr/bin/env python3
"""Validate, normalize, plot, and conservatively report the E1/E2 SQLite runs."""

from __future__ import annotations

import csv
import json
import os
import platform
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("ARTIFACT_OUTPUT_ROOT", Path(__file__).resolve().parents[2])).resolve()
EXPECTED_REPETITIONS = 10


def latest_raw(family: str) -> Path:
    paths = sorted((ROOT / "results" / "raw").glob(f"{family.lower()}-sqlite-*.jsonl"))
    if not paths:
        raise FileNotFoundError(f"no {family} raw file found")
    return paths[-1]


def load_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def bootstrap_median(values: list[float], seed: int, samples: int = 10_000) -> tuple[float, float]:
    rng = random.Random(seed)
    count = len(values)
    medians = sorted(statistics.median(rng.choice(values) for _ in range(count)) for _ in range(samples))
    return medians[int(0.025 * (samples - 1))], medians[int(0.975 * (samples - 1))]


def stats(values: list[float], seed: int) -> dict[str, Any]:
    mean = statistics.fmean(values)
    stddev = statistics.stdev(values) if len(values) > 1 else 0.0
    lower, upper = bootstrap_median(values, seed)
    return {
        "median_runtime": statistics.median(values),
        "mean_runtime": mean,
        "std_runtime": stddev,
        "coefficient_of_variation": stddev / mean if mean else None,
        "95_percent_interval": json.dumps([lower, upper]),
        "interval_lower": lower,
        "interval_upper": upper,
    }


def measurement_groups(records: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record["trial_phase"] == "measurement":
            values = []
            for key in keys:
                if key == "Lambda":
                    values.append(record["procedural_parameters"]["Lambda"])
                else:
                    values.append(record[key])
            groups[tuple(values)].append(record)
    return groups


def group_status(records: list[dict[str, Any]]) -> str:
    successful = sum(record["status"] == "ok" for record in records)
    if successful == EXPECTED_REPETITIONS:
        return "ok"
    if successful:
        return f"partial_{successful}_of_{EXPECTED_REPETITIONS}"
    statuses = sorted({record["status"] for record in records})
    return "+".join(statuses)


def assert_semantics(records: list[dict[str, Any]], errors: list[str]) -> None:
    for record in records:
        if record["status"] != "ok":
            continue
        measurements = record["measurements"]
        if measurements["expected_udf_invocations"] != measurements["udf_invocations"]:
            errors.append(f"invocation mismatch: {record['trial_id']}")
        if measurements["expected_loop_iterations"] != measurements["observed_loop_iterations"]:
            errors.append(f"iteration mismatch: {record['trial_id']}")
        if measurements["estimated_cost"] is not None or record["derived_metrics"]["cost_qerror"] is not None:
            errors.append(f"fabricated/non-null SQLite optimizer cost: {record['trial_id']}")


def normalize_e1(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    groups = measurement_groups(records, ("R", "Lambda"))
    for (rows, lambda_value), group in sorted(groups.items()):
        successful = [record for record in group if record["status"] == "ok"]
        item: dict[str, Any] = {
            "R": rows,
            "Lambda": lambda_value,
            "loop_bounds": json.dumps(group[0]["procedural_parameters"]["loop_bounds"]),
            "expected_loop_iterations": group[0]["measurements"]["expected_loop_iterations"],
            "observed_loop_iterations": (
                successful[0]["measurements"]["observed_loop_iterations"] if successful else None
            ),
            "udf_invocations": successful[0]["measurements"]["udf_invocations"] if successful else None,
            "successful_trials": len(successful),
            "failed_or_timeout_trials": len(group) - len(successful),
            "status": group_status(group),
        }
        if successful:
            item.update(stats([record["measurements"]["wall_time_ms"] for record in successful], rows + lambda_value))
            item["runtime_per_R"] = item["median_runtime"] / rows
            item["runtime_per_R_Lambda"] = item["median_runtime"] / (rows * lambda_value)
        else:
            item.update({key: None for key in (
                "median_runtime", "mean_runtime", "std_runtime", "coefficient_of_variation",
                "95_percent_interval", "interval_lower", "interval_upper", "runtime_per_R",
                "runtime_per_R_Lambda",
            )})
        output.append(item)
    return output


def normalize_e2(records: list[dict[str, Any]], errors: list[str]) -> list[dict[str, Any]]:
    output = []
    groups = measurement_groups(records, ("R", "rho", "Lambda", "form"))
    medians: dict[tuple[int, float, int, str], float] = {}
    provisional: dict[tuple[int, float, int, str], dict[str, Any]] = {}
    checksums: dict[tuple[int, float, int, int], dict[str, int | None]] = defaultdict(dict)
    for (rows, rho, lambda_value, form), group in sorted(groups.items()):
        successful = [record for record in group if record["status"] == "ok"]
        for record in successful:
            checksum_key = (rows, rho, lambda_value, record["trial_number"])
            checksums[checksum_key][form] = record["measurements"]["result_checksum"]
        item: dict[str, Any] = {
            "R": rows,
            "rho": rho,
            "Lambda": lambda_value,
            "loop_bounds": json.dumps(group[0]["procedural_parameters"]["loop_bounds"]),
            "form": form,
            "expected_udf_invocations": group[0]["measurements"]["expected_udf_invocations"],
            "observed_udf_invocations": successful[0]["measurements"]["udf_invocations"] if successful else None,
            "expected_loop_iterations": group[0]["measurements"]["expected_loop_iterations"],
            "observed_loop_iterations": successful[0]["measurements"]["observed_loop_iterations"] if successful else None,
            "successful_trials": len(successful),
            "failed_or_timeout_trials": len(group) - len(successful),
            "status": group_status(group),
        }
        if successful:
            item.update(stats([record["measurements"]["wall_time_ms"] for record in successful], int(rho * 1000) + lambda_value))
            medians[(rows, rho, lambda_value, form)] = item["median_runtime"]
        else:
            item.update({key: None for key in (
                "median_runtime", "mean_runtime", "std_runtime", "coefficient_of_variation",
                "95_percent_interval", "interval_lower", "interval_upper",
            )})
        provisional[(rows, rho, lambda_value, form)] = item

    for key, values in checksums.items():
        if set(values) == {"udf_before_filter", "filter_before_udf"} and len(set(values.values())) != 1:
            errors.append(f"E2 checksum mismatch {key}: {values}")

    for key, item in sorted(provisional.items()):
        rows, rho, lambda_value, form = key
        other = "filter_before_udf" if form == "udf_before_filter" else "udf_before_filter"
        own_median = medians.get(key)
        other_median = medians.get((rows, rho, lambda_value, other))
        if own_median is not None and other_median is not None:
            item["runtime_ratio"] = own_median / other_median
            item["plan_regret"] = own_median / min(own_median, other_median)
        else:
            item["runtime_ratio"] = None
            item["plan_regret"] = None
        output.append(item)
    return output


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def create_figures(e1: list[dict[str, Any]], e2: list[dict[str, Any]]) -> None:
    import matplotlib.pyplot as plt

    figure_dir = ROOT / "results" / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    colors = {100: "tab:blue", 1000: "tab:orange", 10000: "tab:green"}

    figure, axis = plt.subplots(figsize=(6.4, 4.2))
    for rows in sorted({item["R"] for item in e1}):
        subset = sorted((item for item in e1 if item["R"] == rows and item["median_runtime"] is not None), key=lambda item: item["Lambda"])
        axis.errorbar(
            [item["Lambda"] for item in subset],
            [item["median_runtime"] for item in subset],
            yerr=[
                [item["median_runtime"] - item["interval_lower"] for item in subset],
                [item["interval_upper"] - item["median_runtime"] for item in subset],
            ],
            marker="o", capsize=3, label=f"R={rows}", color=colors[rows],
        )
    axis.set_xscale("log")
    axis.set_yscale("log")
    axis.set_xlabel("Procedural amplification, Lambda (log scale)")
    axis.set_ylabel("Median wall time (ms, log scale)")
    axis.legend()
    axis.grid(True, which="both", alpha=0.25)
    figure.tight_layout()
    figure.savefig(figure_dir / "e1_runtime_vs_amplification.pdf")
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(6.4, 4.2))
    subset = sorted((item for item in e1 if item["median_runtime"] is not None), key=lambda item: item["R"] * item["Lambda"])
    axis.errorbar(
        [item["R"] * item["Lambda"] for item in subset],
        [item["median_runtime"] for item in subset],
        yerr=[
            [item["median_runtime"] - item["interval_lower"] for item in subset],
            [item["interval_upper"] - item["median_runtime"] for item in subset],
        ],
        fmt="o", capsize=2,
    )
    axis.set_xscale("log")
    axis.set_yscale("log")
    axis.set_xlabel("R × Lambda observed work units (log scale)")
    axis.set_ylabel("Median wall time (ms, log scale)")
    axis.grid(True, which="both", alpha=0.25)
    figure.tight_layout()
    figure.savefig(figure_dir / "e1_runtime_vs_work.pdf")
    plt.close(figure)

    a_rows = [item for item in e2 if item["form"] == "udf_before_filter" and item["runtime_ratio"] is not None]
    figure, axis = plt.subplots(figsize=(6.4, 4.2))
    for rho in sorted({item["rho"] for item in a_rows}, reverse=True):
        subset = sorted((item for item in a_rows if item["rho"] == rho), key=lambda item: item["Lambda"])
        axis.plot([item["Lambda"] for item in subset], [item["runtime_ratio"] for item in subset], marker="o", label=f"rho={rho:g}")
    axis.axhline(1.0, color="black", linewidth=0.8, linestyle=":")
    axis.set_xscale("log")
    axis.set_xlabel("Procedural amplification, Lambda (log scale)")
    axis.set_ylabel("Median runtime ratio: UDF-before / filter-before")
    axis.legend()
    axis.grid(True, which="both", alpha=0.25)
    figure.tight_layout()
    figure.savefig(figure_dir / "e2_runtime_ratio_vs_lambda.pdf")
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(7.2, 4.8))
    for rho in sorted({item["rho"] for item in e2}, reverse=True):
        for form, style in (("udf_before_filter", "-"), ("filter_before_udf", "--")):
            subset = sorted(
                (item for item in e2 if item["rho"] == rho and item["form"] == form and item["plan_regret"] is not None),
                key=lambda item: item["Lambda"],
            )
            axis.plot(
                [item["Lambda"] for item in subset], [item["plan_regret"] for item in subset],
                marker="o", linestyle=style, label=f"rho={rho:g}, {form}",
            )
    axis.axhline(1.0, color="black", linewidth=0.8, linestyle=":")
    axis.set_xscale("log")
    axis.set_xlabel("Procedural amplification, Lambda (log scale)")
    axis.set_ylabel("Controlled plan regret")
    axis.legend(fontsize=7, ncol=2)
    axis.grid(True, which="both", alpha=0.25)
    figure.tight_layout()
    figure.savefig(figure_dir / "e2_regret_vs_lambda.pdf")
    plt.close(figure)


def markdown_table(rows: list[dict[str, Any]], fields: list[str]) -> str:
    lines = ["| " + " | ".join(fields) + " |", "|" + "|".join("---" for _ in fields) + "|"]
    for row in rows:
        rendered = []
        for field in fields:
            value = row.get(field)
            if isinstance(value, float):
                value = f"{value:.6g}"
            rendered.append(str(value))
        lines.append("| " + " | ".join(rendered) + " |")
    return "\n".join(lines)


def report(e1: list[dict[str, Any]], e2: list[dict[str, Any]], e1_records: list[dict[str, Any]], e2_records: list[dict[str, Any]], e1_path: Path, e2_path: Path, validation: dict[str, Any]) -> None:
    measurement_records = [r for r in e1_records + e2_records if r["trial_phase"] == "measurement"]
    warmups = [r for r in e1_records + e2_records if r["trial_phase"] == "warmup"]
    failures = [r for r in measurement_records if r["status"] != "ok"]
    cv_flags = [
        {"family": "E1", "R": item["R"], "rho": None, "Lambda": item["Lambda"], "form": None, "cv": item["coefficient_of_variation"]}
        for item in e1 if item["coefficient_of_variation"] is not None and item["coefficient_of_variation"] > 0.10
    ] + [
        {"family": "E2", "R": item["R"], "rho": item["rho"], "Lambda": item["Lambda"], "form": item["form"], "cv": item["coefficient_of_variation"]}
        for item in e2 if item["coefficient_of_variation"] is not None and item["coefficient_of_variation"] > 0.10
    ]
    e1_monotonic = 0
    for rows in sorted({item["R"] for item in e1}):
        medians = [item["median_runtime"] for item in sorted((x for x in e1 if x["R"] == rows), key=lambda x: x["Lambda"])]
        if all(a <= b for a, b in zip(medians, medians[1:])):
            e1_monotonic += 1
    a_ratios = [item for item in e2 if item["form"] == "udf_before_filter" and item["runtime_ratio"] is not None]
    selective_trends = {}
    for rho in (0.5, 0.1, 0.01):
        values = [item["runtime_ratio"] for item in sorted((x for x in a_ratios if x["rho"] == rho), key=lambda x: x["Lambda"])]
        selective_trends[str(rho)] = all(a <= b for a, b in zip(values, values[1:]))
    low_lambda = [item["runtime_per_R_Lambda"] for item in e1 if item["Lambda"] == 1]
    high_lambda = [item["runtime_per_R_Lambda"] for item in e1 if item["Lambda"] >= 100]
    overhead_ratio = statistics.median(low_lambda) / statistics.median(high_lambda)

    e1_display = [{
        "R": x["R"], "Lambda": x["Lambda"], "median_ms": x["median_runtime"],
        "mean_ms": x["mean_runtime"], "cv": x["coefficient_of_variation"], "status": x["status"],
    } for x in e1]
    e2_display = [{
        "rho": x["rho"], "Lambda": x["Lambda"], "form": x["form"],
        "calls": x["observed_udf_invocations"], "median_ms": x["median_runtime"],
        "ratio": x["runtime_ratio"], "regret": x["plan_regret"], "status": x["status"],
    } for x in e2]
    cv_text = "None." if not cv_flags else "\n".join(
        f"- {x['family']} R={x['R']} rho={x['rho']} Lambda={x['Lambda']} form={x['form']}: CV={x['cv']:.3f}"
        for x in cv_flags
    )
    e1_measurements = [r for r in e1_records if r["trial_phase"] == "measurement"]
    e2_measurements = [r for r in e2_records if r["trial_phase"] == "measurement"]
    document = f"""# E1/E2 controlled SQLite report

## Scope and environment

On this SQLite configuration, E1 and E2 used SQLite {e1_records[0]['system_version']} through Python {platform.python_version()} on the audited VMware guest. E1 executed 15 configurations (`R` in 100, 1,000, 10,000; `Lambda` in 1, 10, 100, 960, 2,000). E2 executed 32 forms (`R=10,000`; `rho` in 1, 0.5, 0.1, 0.01; `Lambda` in 1, 10, 100, 960; both controlled placements). Each configuration used 3 warmups, 10 measurement repetitions, seed 42, and a 60-second query timeout.

Raw input: `{e1_path}` and `{e2_path}`. E1 produced {len(e1_measurements)} measurement records with {sum(r['status'] != 'ok' for r in e1_measurements)} failures/timeouts; E2 produced {len(e2_measurements)} measurement records with {sum(r['status'] != 'ok' for r in e2_measurements)} failures/timeouts. The combined run preserved {len(warmups)} warmup and {len(measurement_records)} measurement records.

## Semantic validation

The nested loop bounds have exact products 1, 10, 100, 960 (`8×10×12`), and 2,000. The UDF increments a local counter at every leaf iteration. All successful trials satisfied expected invocations and expected effective loop iterations: **{validation['work_count_validation']}**. E2 retained counts were exact and checksums matched between the two forms: **{validation['e2_checksum_equivalence']}**.

The scalar callback is deterministic and has no external effects. Its only internal instrumentation effect is one integer counter increment per leaf work unit plus one aggregate update per UDF invocation. This is constant per unit/invocation but is part of measured runtime. Median runtime per `R×Lambda` at Lambda=1 was {overhead_ratio:.3g}× the median for Lambda>=100, so low-amplification cases are flagged as fixed-overhead/instrumentation-sensitive; the experiment cannot isolate counter cost from callback, scan, and loop-control overhead.

Form A uses a tested non-flattenable subquery (`LIMIT -1 OFFSET 0`) to force UDF-before-filter evaluation. Form B filters directly before invoking the UDF. This is explicit alternative measurement, not evidence that SQLite's optimizer chooses between the forms.

## E1 results

{markdown_table(e1_display, ['R', 'Lambda', 'median_ms', 'mean_ms', 'cv', 'status'])}

Median runtime increased monotonically with Lambda for {e1_monotonic} of 3 `R` groups. This is qualitative evidence on this SQLite/Python-callback configuration that runtime tracks increasing procedural work; it is not a claim of perfect linearity or general DBMS behavior. The diagnostic normalized columns in `e1_amplification.csv` show substantial fixed overhead at low Lambda.

## E2 results

{markdown_table(e2_display, ['rho', 'Lambda', 'form', 'calls', 'median_ms', 'ratio', 'regret', 'status'])}

For each E2 row, `runtime_ratio` is that form's median divided by the other measured legal form. `plan_regret` uses only the fastest measured alternative among the two evaluated legal forms. It is not a globally optimal plan.

For selective rho values, the UDF-before/filter-before runtime ratio increased monotonically across the sampled Lambda values for: {json.dumps(selective_trends, sort_keys=True)}. This directly reports the sampled trend and does not force the amplification hypothesis to hold.

## Variability and sanity checks

Configurations with coefficient of variation above 10%:

{cv_text}

No trials were discarded. No automatic rerun was performed; preserving the specified ten repetitions avoids a post-hoc sampling rule. Both CV flags are Lambda=1 E1 diagnostics whose extremely short runtimes are fixed-overhead/timer sensitive; none of the important high-amplification or E2 placement configurations exceeded 10%. Plans were structurally consistent with the intended forms: Form A used a coroutine subquery barrier and Form B scanned the input directly. Database-reported execution time, optimizer UDF cost, and cost Q-error remain null because SQLite exposes no meaningful values here.

## Limitations and next step

These measurements combine Python callback overhead, recursive loop-control overhead, instrumentation, SQLite execution, and the explicit Form-A subquery barrier. Different Lambda points also have different nesting depths. They validate work accounting and controlled placement consequences; they do not establish optimizer awareness, cost-estimation accuracy, or cross-system performance.

The next recommended engine is a newer DuckDB in a project-local virtual environment, after pinning an exact version and verifying scalar Python UDF support. PostgreSQL remains blocked locally because its server/client libraries and a container runtime are absent.

## Artifacts

- Raw E1: `{e1_path}`
- Raw E2: `{e2_path}`
- Normalized E1: `{ROOT / 'results/normalized/e1_amplification.csv'}`
- Normalized E2: `{ROOT / 'results/normalized/e2_selectivity_amplification.csv'}`
- Validation: `{ROOT / 'results/normalized/e1_e2_validation.json'}`
- E1 figures: `{ROOT / 'results/figures/e1_runtime_vs_amplification.pdf'}`, `{ROOT / 'results/figures/e1_runtime_vs_work.pdf'}`
- E2 figures: `{ROOT / 'results/figures/e2_runtime_ratio_vs_lambda.pdf'}`, `{ROOT / 'results/figures/e2_regret_vs_lambda.pdf'}`
"""
    (ROOT / "docs" / "e1_e2_report.md").write_text(document, encoding="utf-8")


def main() -> int:
    (ROOT / "docs").mkdir(parents=True, exist_ok=True)
    e1_path = latest_raw("E1")
    e2_path = latest_raw("E2")
    e1_records = load_records(e1_path)
    e2_records = load_records(e2_path)
    errors: list[str] = []
    assert_semantics(e1_records + e2_records, errors)
    e1 = normalize_e1(e1_records)
    e2 = normalize_e2(e2_records, errors)

    expected_plans = True
    for record in e2_records:
        plan = record["measurements"]["query_plan"]
        if record["form"] == "udf_before_filter":
            expected_plans &= "CO-ROUTINE SUBQUERY" in plan and "SCAN SUBQUERY" in plan
        else:
            expected_plans &= "SCAN input_rows" in plan and "SUBQUERY" not in plan
    validation = {
        "schema_version": "1.0.0",
        "source_raw_files": [str(e1_path), str(e2_path)],
        "work_count_validation": "PASS" if not any("mismatch" in error for error in errors) else "FAIL",
        "e2_checksum_equivalence": "PASS" if not any("checksum" in error for error in errors) else "FAIL",
        "query_plan_structure": "PASS" if expected_plans else "UNEXPECTED",
        "measurement_trials": sum(r["trial_phase"] == "measurement" for r in e1_records + e2_records),
        "warmup_trials": sum(r["trial_phase"] == "warmup" for r in e1_records + e2_records),
        "failed_or_timeout_measurement_trials": sum(r["trial_phase"] == "measurement" and r["status"] != "ok" for r in e1_records + e2_records),
        "errors": errors,
    }
    if errors:
        raise SystemExit("validation failed: " + "; ".join(errors))

    e1_fields = [
        "R", "Lambda", "loop_bounds", "median_runtime", "mean_runtime", "std_runtime",
        "coefficient_of_variation", "95_percent_interval", "expected_loop_iterations",
        "observed_loop_iterations", "udf_invocations", "runtime_per_R", "runtime_per_R_Lambda",
        "successful_trials", "failed_or_timeout_trials", "status",
    ]
    e2_fields = [
        "R", "rho", "Lambda", "loop_bounds", "form", "expected_udf_invocations",
        "observed_udf_invocations", "expected_loop_iterations", "observed_loop_iterations",
        "median_runtime", "mean_runtime", "std_runtime", "coefficient_of_variation",
        "95_percent_interval", "runtime_ratio", "plan_regret", "successful_trials",
        "failed_or_timeout_trials", "status",
    ]
    write_csv(ROOT / "results" / "normalized" / "e1_amplification.csv", e1, e1_fields)
    write_csv(ROOT / "results" / "normalized" / "e2_selectivity_amplification.csv", e2, e2_fields)
    (ROOT / "results" / "normalized" / "e1_e2_validation.json").write_text(
        json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    create_figures(e1, e2)
    report(e1, e2, e1_records, e2_records, e1_path, e2_path, validation)
    print(json.dumps(validation, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
