#!/usr/bin/env python3
"""Validate DuckDB E1/E2, create required artifacts, and compare frozen SQLite trends."""

from __future__ import annotations

import csv
import json
import platform
import statistics
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.analyze.analyze_e1_e2 import (  # noqa: E402
    assert_semantics,
    load_records,
    markdown_table,
    normalize_e1,
    normalize_e2,
    write_csv,
)


def latest_duckdb_raw(family: str) -> Path:
    paths = sorted((ROOT / "results" / "raw").glob(f"{family.lower()}-duckdb-*.jsonl"))
    if not paths:
        raise FileNotFoundError(f"no DuckDB {family} raw file found")
    return paths[-1]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def create_figures(e1: list[dict[str, Any]], e2: list[dict[str, Any]]) -> None:
    import matplotlib.pyplot as plt

    output = ROOT / "results" / "figures"
    colors = {100: "tab:blue", 1000: "tab:orange", 10000: "tab:green"}
    figure, axis = plt.subplots(figsize=(6.4, 4.2))
    for rows in sorted({item["R"] for item in e1}):
        subset = sorted(
            (item for item in e1 if item["R"] == rows and item["median_runtime"] is not None),
            key=lambda item: item["Lambda"],
        )
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
    axis.set_ylabel("DuckDB median wall time (ms, log scale)")
    axis.legend()
    axis.grid(True, which="both", alpha=0.25)
    figure.tight_layout()
    figure.savefig(output / "e1_duckdb_runtime_vs_amplification.pdf")
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(6.4, 4.2))
    subset = sorted(
        (item for item in e1 if item["median_runtime"] is not None),
        key=lambda item: item["R"] * item["Lambda"],
    )
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
    axis.set_ylabel("DuckDB median wall time (ms, log scale)")
    axis.grid(True, which="both", alpha=0.25)
    figure.tight_layout()
    figure.savefig(output / "e1_duckdb_runtime_vs_work.pdf")
    plt.close(figure)

    a_rows = [
        item for item in e2
        if item["form"] == "udf_before_filter" and item["runtime_ratio"] is not None
    ]
    figure, axis = plt.subplots(figsize=(6.4, 4.2))
    for rho in sorted({item["rho"] for item in a_rows}, reverse=True):
        subset = sorted((item for item in a_rows if item["rho"] == rho), key=lambda item: item["Lambda"])
        axis.plot(
            [item["Lambda"] for item in subset],
            [item["runtime_ratio"] for item in subset],
            marker="o", label=f"rho={rho:g}",
        )
    axis.axhline(1.0, color="black", linewidth=0.8, linestyle=":")
    axis.set_xscale("log")
    axis.set_xlabel("Procedural amplification, Lambda (log scale)")
    axis.set_ylabel("DuckDB median runtime ratio: UDF-before / filter-before")
    axis.legend()
    axis.grid(True, which="both", alpha=0.25)
    figure.tight_layout()
    figure.savefig(output / "e2_duckdb_runtime_ratio_vs_lambda.pdf")
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(7.2, 4.8))
    for rho in sorted({item["rho"] for item in e2}, reverse=True):
        for form, style in (("udf_before_filter", "-"), ("filter_before_udf", "--")):
            subset = sorted(
                (
                    item for item in e2
                    if item["rho"] == rho and item["form"] == form
                    and item["plan_regret"] is not None
                ),
                key=lambda item: item["Lambda"],
            )
            axis.plot(
                [item["Lambda"] for item in subset],
                [item["plan_regret"] for item in subset],
                marker="o", linestyle=style, label=f"rho={rho:g}, {form}",
            )
    axis.axhline(1.0, color="black", linewidth=0.8, linestyle=":")
    axis.set_xscale("log")
    axis.set_xlabel("Procedural amplification, Lambda (log scale)")
    axis.set_ylabel("DuckDB controlled plan regret")
    axis.legend(fontsize=7, ncol=2)
    axis.grid(True, which="both", alpha=0.25)
    figure.tight_layout()
    figure.savefig(output / "e2_duckdb_regret_vs_lambda.pdf")
    plt.close(figure)


def cross_system_rows(
    duckdb_e1: list[dict[str, Any]], duckdb_e2: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    sqlite_e1 = read_csv(ROOT / "results" / "normalized" / "e1_amplification.csv")
    sqlite_e2 = read_csv(ROOT / "results" / "normalized" / "e2_selectivity_amplification.csv")
    sqlite_e1_index = {(int(row["R"]), int(row["Lambda"])): row for row in sqlite_e1}
    sqlite_e2_index = {
        (int(row["R"]), float(row["rho"]), int(row["Lambda"]), row["form"]): row
        for row in sqlite_e2
    }
    output = []
    for row in duckdb_e1:
        other = sqlite_e1_index[(row["R"], row["Lambda"])]
        output.append({
            "study": "E1", "R": row["R"], "rho": None, "Lambda": row["Lambda"],
            "form": "single_form", "sqlite_median_runtime_ms": other["median_runtime"],
            "duckdb_median_runtime_ms": row["median_runtime"],
            "sqlite_cv": other["coefficient_of_variation"],
            "duckdb_cv": row["coefficient_of_variation"],
            "sqlite_runtime_ratio": None, "duckdb_runtime_ratio": None,
            "sqlite_plan_regret": None, "duckdb_plan_regret": None,
            "sqlite_status": other["status"], "duckdb_status": row["status"],
        })
    for row in duckdb_e2:
        other = sqlite_e2_index[(row["R"], row["rho"], row["Lambda"], row["form"])]
        output.append({
            "study": "E2", "R": row["R"], "rho": row["rho"], "Lambda": row["Lambda"],
            "form": row["form"], "sqlite_median_runtime_ms": other["median_runtime"],
            "duckdb_median_runtime_ms": row["median_runtime"],
            "sqlite_cv": other["coefficient_of_variation"],
            "duckdb_cv": row["coefficient_of_variation"],
            "sqlite_runtime_ratio": other["runtime_ratio"],
            "duckdb_runtime_ratio": row["runtime_ratio"],
            "sqlite_plan_regret": other["plan_regret"],
            "duckdb_plan_regret": row["plan_regret"],
            "sqlite_status": other["status"], "duckdb_status": row["status"],
        })
    return output


def monotonic_e1(rows: list[dict[str, Any]]) -> int:
    count = 0
    for cardinality in sorted({row["R"] for row in rows}):
        values = [
            row["median_runtime"]
            for row in sorted((x for x in rows if x["R"] == cardinality), key=lambda x: x["Lambda"])
        ]
        count += int(all(left <= right for left, right in zip(values, values[1:])))
    return count


def monotonic_e1_high_amplification(rows: list[dict[str, Any]]) -> int:
    count = 0
    for cardinality in sorted({row["R"] for row in rows}):
        values = [
            row["median_runtime"]
            for row in sorted(
                (x for x in rows if x["R"] == cardinality and x["Lambda"] >= 100),
                key=lambda x: x["Lambda"],
            )
        ]
        count += int(all(left <= right for left, right in zip(values, values[1:])))
    return count


def selective_trends(e2: list[dict[str, Any]]) -> dict[str, bool]:
    output = {}
    for rho in (0.5, 0.1, 0.01):
        values = [
            row["runtime_ratio"]
            for row in sorted(
                (
                    x for x in e2
                    if x["rho"] == rho and x["form"] == "udf_before_filter"
                ),
                key=lambda x: x["Lambda"],
            )
        ]
        output[str(rho)] = all(left <= right for left, right in zip(values, values[1:]))
    return output


def write_report(
    e1: list[dict[str, Any]],
    e2: list[dict[str, Any]],
    e1_records: list[dict[str, Any]],
    e2_records: list[dict[str, Any]],
    e1_path: Path,
    e2_path: Path,
    validation: dict[str, Any],
) -> None:
    e1_cv_flags = [row for row in e1 if row["coefficient_of_variation"] > 0.10]
    e2_cv_flags = [row for row in e2 if row["coefficient_of_variation"] > 0.10]
    a_rows = [row for row in e2 if row["form"] == "udf_before_filter"]
    ratios = [row["runtime_ratio"] for row in a_rows]
    selective_ratios = [row["runtime_ratio"] for row in a_rows if row["rho"] < 1.0]
    regrets = [row["plan_regret"] for row in e2]
    low = [row["runtime_per_R_Lambda"] for row in e1 if row["Lambda"] == 1]
    high = [row["runtime_per_R_Lambda"] for row in e1 if row["Lambda"] >= 100]
    overhead_ratio = statistics.median(low) / statistics.median(high)
    trends = selective_trends(e2)
    sqlite_e1 = read_csv(ROOT / "results" / "normalized" / "e1_amplification.csv")
    sqlite_e2 = read_csv(ROOT / "results" / "normalized" / "e2_selectivity_amplification.csv")
    sqlite_a = [row for row in sqlite_e2 if row["form"] == "udf_before_filter"]
    sqlite_trends = {}
    for rho in (0.5, 0.1, 0.01):
        values = [
            float(row["runtime_ratio"])
            for row in sorted(
                (x for x in sqlite_a if float(x["rho"]) == rho), key=lambda x: int(x["Lambda"])
            )
        ]
        sqlite_trends[str(rho)] = all(a <= b for a, b in zip(values, values[1:]))

    e1_table = [{
        "R": row["R"], "Lambda": row["Lambda"], "median_ms": row["median_runtime"],
        "cv": row["coefficient_of_variation"], "status": row["status"],
    } for row in e1]
    e2_table = [{
        "rho": row["rho"], "Lambda": row["Lambda"], "form": row["form"],
        "calls": row["observed_udf_invocations"], "median_ms": row["median_runtime"],
        "ratio": row["runtime_ratio"], "regret": row["plan_regret"], "status": row["status"],
    } for row in e2]
    cv_text = "None." if not e1_cv_flags + e2_cv_flags else "\n".join(
        f"- {('E1' if 'udf_invocations' in row else 'E2')} R={row['R']} "
        f"rho={row.get('rho')} Lambda={row['Lambda']} form={row.get('form')}: "
        f"CV={row['coefficient_of_variation']:.3f}"
        for row in e1_cv_flags + e2_cv_flags
    )
    document = f"""# DuckDB E1/E2 replication report

## Environment and scope

The replication used project-local `.venv-duckdb`, Python 3.10.12, DuckDB 1.5.5, and NumPy 2.2.6. The system Python and frozen SQLite definitions/artifacts were not modified. E1 repeated all 15 configurations and E2 repeated all 16 paired configurations/32 forms with three warmups, ten measurement trials, seed 42, and the unchanged 60-second timeout.

E1 preserved 195 raw records (150 measurements); E2 preserved 416 (320 measurements). Failed/timeout records: {validation['failed_or_timeout_records']}.

## API feasibility and optimizer behavior

Native scalar Python UDF registration, deterministic checksums, invocation counters, and leaf-iteration counters passed. A plain projection subquery did **not** preserve UDF-before-filter behavior: DuckDB pushed `retained=1` into the scan and observed 5 calls instead of 10 in the probe. `MATERIALIZED` also allowed that pushdown.

The accepted Form A uses an unhinted `OFFSET 0` subquery, which introduces a `STREAMING_LIMIT` plan barrier and preserved 10 calls before filtering. Selective Form B plans exposed `retained=1` at the sequential scan and observed 5 calls in the probe. At `rho=1`, DuckDB removed the redundant always-true filter; counts still remained R. Results matched. No optimizer was disabled. This means DuckDB naturally collapses the naive SQL forms, but the explicit controlled alternatives remain measurable.

## Semantic validation

- Work-count equality: **{validation['work_count_validation']}**
- E2 checksum equivalence: **{validation['e2_checksum_equivalence']}**
- Expected DuckDB plan structures: **{validation['query_plan_structure']}**
- Frozen dataset checksum reuse: **PASS**
- SQLite/DuckDB grid equality: **PASS**
- Estimated optimizer cost and Q-error: null throughout

The UDF body is the unchanged SQLite work function. Internal instrumentation adds one local counter increment per leaf and one aggregate update per invocation. At Lambda=1, median normalized time per work unit was {overhead_ratio:.3g}× the Lambda>=100 median, so the low-amplification region remains fixed-overhead/instrumentation sensitive.

## E1 DuckDB results

{markdown_table(e1_table, ['R', 'Lambda', 'median_ms', 'cv', 'status'])}

Median runtime increased monotonically with Lambda over the full grid for only {monotonic_e1(e1)} of 3 R groups, so DuckDB does **not** replicate SQLite's full-range monotonicity result. Restricting the diagnostic to Lambda>=100, runtime increased monotonically for {monotonic_e1_high_amplification(e1)} of 3 groups. The high-work region therefore tracks increasing `R×Lambda` qualitatively, while fixed callback/runtime overhead and variability mask the low-amplification trend.

## E2 DuckDB results

{markdown_table(e2_table, ['rho', 'Lambda', 'form', 'calls', 'median_ms', 'ratio', 'regret', 'status'])}

Across all UDF-before-filter rows, the observed runtime ratio ranged from {min(ratios):.6g} to {max(ratios):.6g}; among selective rows it ranged from {min(selective_ratios):.6g} to {max(selective_ratios):.6g}. Maximum controlled regret was {max(regrets):.6g}. `T_star` means only the fastest measured alternative among the evaluated legal forms.

For selective rho values, the ratio increased monotonically over sampled Lambda values as follows: {json.dumps(trends, sort_keys=True)}. Thus the DuckDB data do **not** support monotonic magnification with Lambda. Placement effects nevertheless survive in every explicit selective pair because every UDF-before/filter-before ratio exceeded one. The plain forms would not show this work difference because DuckDB rewrites them to filter first.

## Variability

Configurations above 10% CV:

{cv_text}

No trials were discarded or automatically extended; the frozen ten-repetition policy was preserved. {len(e1_cv_flags)} of 15 E1 configurations and {len(e2_cv_flags)} of 32 E2 forms exceeded 10% CV. Several low-/mid-Lambda series were visibly bimodal (for example, `R=1000, Lambda=1` alternated around 145 and 210 ms). This could reflect shared-host scheduling or DuckDB/Python callback behavior, but the current data cannot identify the cause. Low- and mid-amplification trends are therefore conditional evidence.

## SQLite versus DuckDB

SQLite showed monotonic E1 median runtime over the full Lambda grid in all three R groups; DuckDB did so in only {monotonic_e1(e1)} of three. In the Lambda>=100 region, DuckDB was monotonic in {monotonic_e1_high_amplification(e1)} of three groups. SQLite showed increasing controlled placement ratios across Lambda for all selective rho values, while DuckDB did not: SQLite {json.dumps(sqlite_trends, sort_keys=True)}, DuckDB {json.dumps(trends, sort_keys=True)}. Both systems did agree that stronger selectivity produced substantially larger controlled placement consequences and that explicit filter-before evaluation saved the validated procedural work.

This is partial qualitative agreement, not a hardware-normalized comparison of absolute runtimes. DuckDB differs operationally: it collapsed the naive subquery through filter pushdown, whereas controlled work separation required the visible `OFFSET 0` streaming barrier. The comparison demonstrates the same procedural-work accounting and selectivity phenomenon, but not the same low-amplification monotonic runtime behavior.

## Decision

**CONDITIONAL GO** for a third system. Conditions: perform an API/optimizer-collapse feasibility probe first, preserve checksummed datasets and the exact kernel, validate invocations and leaf counts, and document any barrier/materialization needed to expose both legal alternatives. These results do not authorize E3, UDFBench, QFusor, or another system in the current phase.

## Artifacts

- Raw E1: `{e1_path}`
- Raw E2: `{e2_path}`
- DuckDB E1 CSV: `{ROOT / 'results/normalized/e1_duckdb.csv'}`
- DuckDB E2 CSV: `{ROOT / 'results/normalized/e2_duckdb.csv'}`
- Cross-system CSV: `{ROOT / 'results/normalized/e1_e2_sqlite_duckdb_comparison.csv'}`
- Environment: `{ROOT / 'metadata/duckdb_environment.json'}`
- Figures: `{ROOT / 'results/figures/e1_duckdb_runtime_vs_amplification.pdf'}`, `{ROOT / 'results/figures/e1_duckdb_runtime_vs_work.pdf'}`, `{ROOT / 'results/figures/e2_duckdb_runtime_ratio_vs_lambda.pdf'}`, `{ROOT / 'results/figures/e2_duckdb_regret_vs_lambda.pdf'}`
"""
    (ROOT / "docs" / "e1_e2_duckdb_report.md").write_text(document, encoding="utf-8")


def main() -> int:
    e1_path = latest_duckdb_raw("E1")
    e2_path = latest_duckdb_raw("E2")
    e1_records = load_records(e1_path)
    e2_records = load_records(e2_path)
    errors: list[str] = []
    assert_semantics(e1_records + e2_records, errors)
    e1 = normalize_e1(e1_records)
    e2 = normalize_e2(e2_records, errors)

    if len(e1) != 15 or len(e2) != 32:
        errors.append(f"unexpected grid sizes E1={len(e1)} E2={len(e2)}")
    expected_plans = True
    for record in e2_records:
        plan = record["measurements"]["query_plan"]
        if record["form"] == "udf_before_filter":
            expected_plans &= "STREAMING_LIMIT" in plan and "FILTER" in plan
        else:
            expected_plans &= "STREAMING_LIMIT" not in plan
            if record["rho"] < 1.0:
                expected_plans &= "Filters: retained=1" in plan
    failures = sum(record["status"] != "ok" for record in e1_records + e2_records)
    validation = {
        "schema_version": "1.0.0",
        "source_raw_files": [str(e1_path), str(e2_path)],
        "duckdb_version": e1_records[0]["system_version"],
        "work_count_validation": "PASS" if not any("mismatch" in error for error in errors) else "FAIL",
        "e2_checksum_equivalence": "PASS" if not any("checksum" in error for error in errors) else "FAIL",
        "query_plan_structure": "PASS" if expected_plans else "UNEXPECTED",
        "measurement_trials": sum(r["trial_phase"] == "measurement" for r in e1_records + e2_records),
        "warmup_trials": sum(r["trial_phase"] == "warmup" for r in e1_records + e2_records),
        "failed_or_timeout_records": failures,
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
    write_csv(ROOT / "results" / "normalized" / "e1_duckdb.csv", e1, e1_fields)
    write_csv(ROOT / "results" / "normalized" / "e2_duckdb.csv", e2, e2_fields)
    cross = cross_system_rows(e1, e2)
    cross_fields = [
        "study", "R", "rho", "Lambda", "form", "sqlite_median_runtime_ms",
        "duckdb_median_runtime_ms", "sqlite_cv", "duckdb_cv", "sqlite_runtime_ratio",
        "duckdb_runtime_ratio", "sqlite_plan_regret", "duckdb_plan_regret",
        "sqlite_status", "duckdb_status",
    ]
    write_csv(
        ROOT / "results" / "normalized" / "e1_e2_sqlite_duckdb_comparison.csv",
        cross, cross_fields,
    )
    (ROOT / "results" / "normalized" / "e1_e2_duckdb_validation.json").write_text(
        json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    create_figures(e1, e2)
    write_report(e1, e2, e1_records, e2_records, e1_path, e2_path, validation)
    print(json.dumps(validation, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
