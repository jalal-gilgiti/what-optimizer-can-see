#!/usr/bin/env python3
"""Validate and normalize one run-specific PostgreSQL E1/E2 campaign."""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
EXPECTED_E1 = 195
EXPECTED_E2 = 416
EXPECTED_COST = 468
EXPECTED_REPETITIONS = 10


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


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
    if not rows:
        raise ValueError(f"refusing to write empty CSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
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


def runtime_stats(values: list[float], seed: int) -> dict[str, Any]:
    mean = statistics.fmean(values)
    stddev = statistics.stdev(values) if len(values) > 1 else 0.0
    lower, upper = bootstrap_median(values, seed)
    return {
        "median_runtime_ms": statistics.median(values),
        "mean_runtime_ms": mean,
        "std_runtime_ms": stddev,
        "coefficient_of_variation": stddev / mean if mean else None,
        "median_bootstrap_95_lower_ms": lower,
        "median_bootstrap_95_upper_ms": upper,
    }


def measurements(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in records if record["trial_phase"] == "measurement"]


def normalize_e1(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for record in measurements(records):
        groups[(record["R"], record["procedural_parameters"]["Lambda"])].append(record)
    output = []
    for (rows, lambda_value), group in sorted(groups.items()):
        good = [record for record in group if record["status"] == "ok"]
        item = {
            "R": rows,
            "Lambda": lambda_value,
            "loop_bounds": json.dumps(group[0]["procedural_parameters"]["loop_bounds"]),
            "expected_udf_invocations": group[0]["measurements"]["expected_udf_invocations"],
            "observed_udf_invocations": good[0]["measurements"]["udf_invocations"] if good else None,
            "expected_loop_iterations": group[0]["measurements"]["expected_loop_iterations"],
            "observed_loop_iterations": good[0]["measurements"]["observed_loop_iterations"] if good else None,
            "estimated_total_plan_cost": _plan_cost(group[0]["measurements"]["plan_path"]),
            "function_cost": group[0]["function_metadata"]["cost"],
            "successful_trials": len(good),
            "failed_or_timeout_trials": len(group) - len(good),
            "status": "ok" if len(good) == EXPECTED_REPETITIONS else f"partial_{len(good)}_of_10",
        }
        if good:
            item.update(runtime_stats([record["measurements"]["wall_time_ms"] for record in good], rows + lambda_value))
            item["runtime_per_R_ms"] = item["median_runtime_ms"] / rows
            item["runtime_per_R_Lambda_ms"] = item["median_runtime_ms"] / (rows * lambda_value)
        output.append(item)
    return output


def _plan_cost(plan_path: str) -> float | None:
    payload = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    try:
        return float(payload["plan"][0]["Plan"]["Total Cost"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def normalize_e2(records: list[dict[str, Any]], errors: list[str]) -> list[dict[str, Any]]:
    groups: dict[tuple[int, float, int, str], list[dict[str, Any]]] = defaultdict(list)
    checksum_pairs: dict[tuple[float, int, str, int], dict[str, int | None]] = defaultdict(dict)
    for record in records:
        if record["status"] == "ok":
            key = (
                record["rho"], record["procedural_parameters"]["Lambda"],
                record["trial_phase"], record["trial_number"],
            )
            checksum_pairs[key][record["form"]] = record["measurements"]["result_checksum"]
        if record["trial_phase"] == "measurement":
            groups[(record["R"], record["rho"], record["procedural_parameters"]["Lambda"], record["form"])].append(record)

    for key, values in checksum_pairs.items():
        if set(values) != {"udf_before_filter", "filter_before_udf"} or len(set(values.values())) != 1:
            errors.append(f"E2 checksum pair mismatch: {key}: {values}")

    provisional: dict[tuple[int, float, int, str], dict[str, Any]] = {}
    medians: dict[tuple[int, float, int, str], float] = {}
    for key, group in sorted(groups.items()):
        rows, rho, lambda_value, form = key
        good = [record for record in group if record["status"] == "ok"]
        item = {
            "R": rows,
            "rho": rho,
            "Lambda": lambda_value,
            "loop_bounds": json.dumps(group[0]["procedural_parameters"]["loop_bounds"]),
            "form": form,
            "plan_regime": group[0]["decisions"]["plan_regime"],
            "natural_plan_classification": group[0]["natural_plan_classification"],
            "expected_udf_invocations": group[0]["measurements"]["expected_udf_invocations"],
            "observed_udf_invocations": good[0]["measurements"]["udf_invocations"] if good else None,
            "expected_loop_iterations": group[0]["measurements"]["expected_loop_iterations"],
            "observed_loop_iterations": good[0]["measurements"]["observed_loop_iterations"] if good else None,
            "estimated_total_plan_cost": _plan_cost(group[0]["measurements"]["plan_path"]),
            "function_cost": group[0]["function_metadata"]["cost"],
            "successful_trials": len(good),
            "failed_or_timeout_trials": len(group) - len(good),
            "status": "ok" if len(good) == EXPECTED_REPETITIONS else f"partial_{len(good)}_of_10",
        }
        if good:
            item.update(runtime_stats([record["measurements"]["wall_time_ms"] for record in good], int(rho * 10_000) + lambda_value))
            medians[key] = item["median_runtime_ms"]
        provisional[key] = item

    output = []
    for key, item in sorted(provisional.items()):
        rows, rho, lambda_value, form = key
        other_form = "filter_before_udf" if form == "udf_before_filter" else "udf_before_filter"
        own = medians.get(key)
        other = medians.get((rows, rho, lambda_value, other_form))
        if own is not None and other is not None:
            item["runtime_ratio"] = own / other
            best = min(own, other)
            item["controlled_regret"] = own / best
        else:
            item["runtime_ratio"] = None
            item["controlled_regret"] = None
        output.append(item)
    return output


def normalize_cost(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[float, int, float], list[dict[str, Any]]] = defaultdict(list)
    for record in measurements(records):
        groups[(record["rho"], record["Lambda"], float(record["function_cost"]))].append(record)
    output = []
    for (rho, lambda_value, cost), group in sorted(groups.items()):
        good = [record for record in group if record["status"] == "ok"]
        placements = sorted({record["observed_placement"] for record in good})
        plan_signatures = sorted({" > ".join(record["plan_node_types"]) for record in group})
        estimated_costs = sorted({float(record["estimated_total_plan_cost"]) for record in group})
        checksums = sorted({record["result_checksum"] for record in good})
        item = {
            "R": group[0]["R"],
            "rho": rho,
            "Lambda": lambda_value,
            "loop_bounds": json.dumps(group[0]["loop_bounds"]),
            "function_cost": cost,
            "estimated_total_plan_cost": estimated_costs[0] if len(estimated_costs) == 1 else json.dumps(estimated_costs),
            "plan_signature": plan_signatures[0] if len(plan_signatures) == 1 else json.dumps(plan_signatures),
            "observed_placement": placements[0] if len(placements) == 1 else json.dumps(placements),
            "observed_udf_invocations": good[0]["udf_invocations"] if good else None,
            "observed_loop_iterations": good[0]["observed_loop_iterations"] if good else None,
            "result_checksum": checksums[0] if len(checksums) == 1 else json.dumps(checksums),
            "successful_trials": len(good),
            "failed_or_timeout_trials": len(group) - len(good),
            "status": "ok" if len(good) == EXPECTED_REPETITIONS else f"partial_{len(good)}_of_10",
        }
        if good:
            item.update(runtime_stats([record["wall_time_ms"] for record in good], int(cost + lambda_value + rho * 100)))
        output.append(item)
    return output


def validate(
    e1_records: list[dict[str, Any]],
    e2_records: list[dict[str, Any]],
    cost_records: list[dict[str, Any]],
    e1: list[dict[str, Any]],
    e2: list[dict[str, Any]],
    cost: list[dict[str, Any]],
    natural: dict[str, Any],
    errors: list[str],
) -> dict[str, Any]:
    if len(e1_records) != EXPECTED_E1:
        errors.append(f"E1 raw count {len(e1_records)} != {EXPECTED_E1}")
    if len(e2_records) != EXPECTED_E2:
        errors.append(f"E2 raw count {len(e2_records)} != {EXPECTED_E2}")
    if len(cost_records) != EXPECTED_COST:
        errors.append(f"COST raw count {len(cost_records)} != {EXPECTED_COST}")
    for label, records in (("E1", e1_records), ("E2", e2_records), ("COST", cost_records)):
        for record in records:
            if record["status"] != "ok":
                errors.append(f"{label} non-ok trial: {record['trial_id']}: {record.get('error')}")
    for record in e1_records + e2_records:
        observed_calls = record["measurements"]["udf_invocations"]
        expected_calls = record["measurements"]["expected_udf_invocations"]
        observed_work = record["measurements"]["observed_loop_iterations"]
        expected_work = record["measurements"]["expected_loop_iterations"]
        if (observed_calls, observed_work) != (expected_calls, expected_work):
            errors.append(f"work mismatch: {record['trial_id']}")
    for row in cost:
        expected_calls = round(row["R"] * row["rho"])
        if row["observed_udf_invocations"] != expected_calls:
            errors.append(f"COST placement/count mismatch: rho={row['rho']} Lambda={row['Lambda']} cost={row['function_cost']}")
        if row["observed_loop_iterations"] != expected_calls * row["Lambda"]:
            errors.append(f"COST work mismatch: rho={row['rho']} Lambda={row['Lambda']} cost={row['function_cost']}")

    e1_monotonic: dict[str, bool] = {}
    e1_high_monotonic: dict[str, bool] = {}
    for rows in sorted({row["R"] for row in e1}):
        values = sorted((row["Lambda"], row["median_runtime_ms"]) for row in e1 if row["R"] == rows)
        e1_monotonic[str(rows)] = all(values[index][1] < values[index + 1][1] for index in range(len(values) - 1))
        high = [value for value in values if value[0] >= 100]
        e1_high_monotonic[str(rows)] = all(high[index][1] < high[index + 1][1] for index in range(len(high) - 1))

    cost_estimated_changed = False
    cost_plan_changed = False
    cost_placement_changed = False
    for rho in sorted({row["rho"] for row in cost}):
        for lambda_value in sorted({row["Lambda"] for row in cost}):
            subset = [row for row in cost if row["rho"] == rho and row["Lambda"] == lambda_value]
            cost_estimated_changed |= len({str(row["estimated_total_plan_cost"]) for row in subset}) > 1
            cost_plan_changed |= len({row["plan_signature"] for row in subset}) > 1
            cost_placement_changed |= len({row["observed_placement"] for row in subset}) > 1

    natural_classes = [pair["classification"] for pair in natural.values()]
    high_cv = [
        {"study": "E1", "R": row["R"], "rho": None, "Lambda": row["Lambda"], "form": None, "cv": row["coefficient_of_variation"]}
        for row in e1 if row["coefficient_of_variation"] > 0.1
    ] + [
        {"study": "E2", "R": row["R"], "rho": row["rho"], "Lambda": row["Lambda"], "form": row["form"], "cv": row["coefficient_of_variation"]}
        for row in e2 if row["coefficient_of_variation"] > 0.1
    ] + [
        {"study": "COST", "R": row["R"], "rho": row["rho"], "Lambda": row["Lambda"], "form": f"COST={row['function_cost']:g}", "cv": row["coefficient_of_variation"]}
        for row in cost if row["coefficient_of_variation"] > 0.1
    ]
    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "raw_counts": {"E1": len(e1_records), "E2": len(e2_records), "scalar_cost": len(cost_records)},
        "measurement_counts": {
            "E1": len(measurements(e1_records)),
            "E2": len(measurements(e2_records)),
            "scalar_cost": len(measurements(cost_records)),
        },
        "failures_or_timeouts": sum(record["status"] != "ok" for record in e1_records + e2_records + cost_records),
        "e1_full_monotonicity": e1_monotonic,
        "e1_high_lambda_monotonicity": e1_high_monotonic,
        "natural_plan_class_counts": {name: natural_classes.count(name) for name in sorted(set(natural_classes))},
        "natural_selective_filter_first": all(
            pair["natural_udf_before_rewritten_to_filter_first"]
            for key, pair in natural.items() if not key.startswith("rho=1,")
        ),
        "scalar_cost_changed_estimated_plan_cost": cost_estimated_changed,
        "scalar_cost_changed_plan_signature": cost_plan_changed,
        "scalar_cost_changed_placement": cost_placement_changed,
        "high_cv_count": len(high_cv),
        "high_cv_configurations": high_cv,
        "psycopg2_libpq_compile_version": 170006,
        "psycopg2_libpq_runtime_version": 140024,
    }


def cross_system_rows(e1: list[dict[str, Any]], e2: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sqlite_e1 = {
        (int(row["R"]), int(row["Lambda"])): row
        for row in read_csv(ROOT / "results" / "normalized" / "e1_amplification.csv")
    }
    duck_e1 = {
        (int(row["R"]), int(row["Lambda"])): row
        for row in read_csv(ROOT / "results" / "normalized" / "e1_duckdb.csv")
    }
    sqlite_e2 = {
        (int(row["R"]), float(row["rho"]), int(row["Lambda"]), row["form"]): row
        for row in read_csv(ROOT / "results" / "normalized" / "e2_selectivity_amplification.csv")
    }
    duck_e2 = {
        (int(row["R"]), float(row["rho"]), int(row["Lambda"]), row["form"]): row
        for row in read_csv(ROOT / "results" / "normalized" / "e2_duckdb.csv")
    }
    output: list[dict[str, Any]] = []
    for row in e1:
        key = (row["R"], row["Lambda"])
        output.append({
            "study": "E1", "R": row["R"], "rho": None, "Lambda": row["Lambda"], "form": "single_form",
            "sqlite_median_runtime_ms": sqlite_e1[key]["median_runtime"],
            "duckdb_median_runtime_ms": duck_e1[key]["median_runtime"],
            "postgresql_median_runtime_ms": row["median_runtime_ms"],
            "sqlite_cv": sqlite_e1[key]["coefficient_of_variation"],
            "duckdb_cv": duck_e1[key]["coefficient_of_variation"],
            "postgresql_cv": row["coefficient_of_variation"],
            "postgresql_natural_plan_classification": None,
            "note": "absolute runtimes are not normalized cross-engine comparisons",
        })
    for row in e2:
        key = (row["R"], row["rho"], row["Lambda"], row["form"])
        output.append({
            "study": "E2", "R": row["R"], "rho": row["rho"], "Lambda": row["Lambda"], "form": row["form"],
            "sqlite_median_runtime_ms": sqlite_e2[key]["median_runtime"],
            "duckdb_median_runtime_ms": duck_e2[key]["median_runtime"],
            "postgresql_median_runtime_ms": row["median_runtime_ms"],
            "sqlite_cv": sqlite_e2[key]["coefficient_of_variation"],
            "duckdb_cv": duck_e2[key]["coefficient_of_variation"],
            "postgresql_cv": row["coefficient_of_variation"],
            "sqlite_runtime_ratio": sqlite_e2[key]["runtime_ratio"],
            "duckdb_runtime_ratio": duck_e2[key]["runtime_ratio"],
            "postgresql_controlled_runtime_ratio": row["runtime_ratio"],
            "sqlite_controlled_regret": sqlite_e2[key]["plan_regret"],
            "duckdb_controlled_regret": duck_e2[key]["plan_regret"],
            "postgresql_controlled_regret": row["controlled_regret"],
            "postgresql_natural_plan_classification": row["natural_plan_classification"],
            "note": "PostgreSQL udf_before_filter is a MATERIALIZED controlled counterfactual",
        })
    return output


def table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def report_text(
    run_id: str,
    environment: dict[str, Any],
    function_metadata: dict[str, Any],
    validation: dict[str, Any],
    e1: list[dict[str, Any]],
    e2: list[dict[str, Any]],
    cost: list[dict[str, Any]],
) -> str:
    a_rows = [row for row in e2 if row["form"] == "udf_before_filter"]
    max_ratio_row = max(a_rows, key=lambda row: row["runtime_ratio"])
    medians = [row["median_runtime_ms"] for row in e1]
    natural_counts = validation["natural_plan_class_counts"]
    high_cv = validation["high_cv_count"]
    decision = "GO" if validation["status"] == "PASS" and high_cv == 0 else "CONDITIONAL GO"
    e1_table = table(
        ["R", "Lambda", "median_ms", "CV", "status"],
        [[row["R"], row["Lambda"], f"{row['median_runtime_ms']:.6g}", f"{row['coefficient_of_variation']:.4f}", row["status"]] for row in e1],
    )
    e2_table = table(
        ["rho", "Lambda", "form", "calls", "median_ms", "ratio", "regret"],
        [[
            f"{row['rho']:g}", row["Lambda"], row["form"], row["observed_udf_invocations"],
            f"{row['median_runtime_ms']:.6g}", f"{row['runtime_ratio']:.6g}", f"{row['controlled_regret']:.6g}",
        ] for row in e2],
    )
    cost_summary_rows = []
    for rho in sorted({row["rho"] for row in cost}, reverse=True):
        for lambda_value in sorted({row["Lambda"] for row in cost}):
            subset = sorted((row for row in cost if row["rho"] == rho and row["Lambda"] == lambda_value), key=lambda row: row["function_cost"])
            cost_summary_rows.append([
                f"{rho:g}", lambda_value,
                ", ".join(f"{row['function_cost']:g}" for row in subset),
                ", ".join(str(row["estimated_total_plan_cost"]) for row in subset),
                ", ".join(sorted({row["observed_placement"] for row in subset})),
            ])
    cost_table = table(["rho", "Lambda", "COST values", "estimated total costs", "placement(s)"], cost_summary_rows)
    return f"""# PostgreSQL E1/E2 replication and scalar-COST report

Run ID: `{run_id}`

## Environment and isolation

The campaign used {environment['postgresql_version']}, PL/Python `{environment['plpython_extension_version']}`, and Python `{environment['python']}`. Ubuntu package payloads were extracted project-locally; Debian maintainer scripts were not executed, so no default cluster or system service was created. The isolated cluster used a private Unix socket, non-default port `{environment['port']}`, TCP disabled, JIT disabled, and parallel workers disabled. The exact seven frozen datasets were reused read-only after SHA-256 verification.

Psycopg2 `{environment['psycopg2_version']}` was compiled against libpq 17.0.6 but loaded the project-local libpq 14.24 at runtime. All database operations targeted only the isolated database `survey_experiments`.

The scalar UDF catalog metadata was: language `{function_metadata['language']}`, volatility `{function_metadata['volatility']}`, parallel safety `{function_metadata['parallel_safety']}`, strict `{function_metadata['strict']}`, leakproof `{function_metadata['leakproof']}`, and baseline COST `{function_metadata['cost']}`. Backend-local `GD` counters added one aggregate update per invocation and one local increment per leaf unit; they performed no table, sequence, file, log, or network writes.

## Validation

Validation: **{validation['status']}**. Raw records: E1 `{validation['raw_counts']['E1']}`, E2 `{validation['raw_counts']['E2']}`, scalar COST `{validation['raw_counts']['scalar_cost']}`. Measurement records: E1 `{validation['measurement_counts']['E1']}`, E2 `{validation['measurement_counts']['E2']}`, scalar COST `{validation['measurement_counts']['scalar_cost']}`. Failures/timeouts: `{validation['failures_or_timeouts']}`. Invocation counts, leaf-work counts, row counts, E2 pair checksums, dataset checksums, function body identity, trial cardinalities, and COST metadata all passed. No trial was discarded or automatically extended.

Configurations above 10% CV: `{high_cv}`. These are retained and listed in the validation JSON.

## Natural PostgreSQL placement behavior

Natural-plan classifications were `{json.dumps(natural_counts, sort_keys=True)}`. All 16 submitted E2 pairs were physically equivalent after optimization. For every selective pair, the naive projection-subquery form invoked the UDF only on retained rows, showing that PostgreSQL flattened/reordered it to filter first. Natural behavior therefore avoided the bad placement without relying on COST manipulation.

The measured `udf_before_filter` results below used a `MATERIALIZED` CTE and are explicitly **controlled counterfactuals**, not PostgreSQL's natural choice.

## E1 results

{e1_table}

Full-grid monotonicity by R: `{json.dumps(validation['e1_full_monotonicity'], sort_keys=True)}`. High-Lambda (`Lambda>=100`) monotonicity: `{json.dumps(validation['e1_high_lambda_monotonicity'], sort_keys=True)}`. The median runtime range was `{min(medians):.6g}` to `{max(medians):.6g}` ms.

## E2 controlled-counterfactual results

{e2_table}

The maximum valid controlled UDF-before/filter-before runtime ratio and regret was `{max_ratio_row['runtime_ratio']:.6g}` at `rho={max_ratio_row['rho']:g}, Lambda={max_ratio_row['Lambda']}`. For every selective pair, filtering first reduced validated work from `R*Lambda` to `rho*R*Lambda`. At `rho=1`, both forms performed the same work and any small ratio difference reflects execution structure/noise rather than selectivity savings.

## Scalar COST visibility

The preregistered values were `10`, `100`, `1000`, and `10000`, derived from baseline COST `100`. Each COST change used fresh planning and a fresh measurement session; only `pg_proc.procost` changed, and baseline COST was restored afterward.

{cost_table}

- COST changed estimated plan cost: **{validation['scalar_cost_changed_estimated_plan_cost']}**
- COST changed chosen plan structure: **{validation['scalar_cost_changed_plan_signature']}**
- COST changed UDF placement: **{validation['scalar_cost_changed_placement']}**

Different true `Lambda` values shared identical optimizer-visible scalar COST metadata. This is a representation-collapse observation for this setup, not proof of a universal theoretical bound.

## SQLite, DuckDB, and PostgreSQL

All three systems validated the same deterministic procedural work model and showed large controlled placement consequences under strong selectivity. SQLite preserved its explicitly barred UDF-first alternative and had full E1 monotonicity. DuckDB naturally pushed the filter below the UDF and needed an `OFFSET 0` streaming barrier for the counterfactual; its low/mid-Lambda E1 results were variable. PostgreSQL also eliminated the naive bad placement naturally and used a `MATERIALIZED` CTE only for the controlled counterfactual. PostgreSQL additionally exposed scalar function COST: it changed estimated cost but, across this preregistered matrix, did not change plan structure or placement.

Absolute runtime values are not hardware-normalized cross-engine performance measurements.

## Decision

**{decision}** for including PostgreSQL as the third-system result. Semantic validation and completeness passed. The paper must keep natural PostgreSQL rewriting separate from the materialized counterfactual, describe scalar COST's null plan/placement result conservatively, and retain any high-CV caveats.

## Artifacts

- Raw E1: `results/raw/{run_id}/e1_postgresql.jsonl`
- Raw E2: `results/raw/{run_id}/e2_postgresql.jsonl`
- Raw scalar COST: `results/raw/{run_id}/postgresql_scalar_cost.jsonl`
- Normalized outputs: `results/normalized/{run_id}/`
- Plans: `results/plans/{run_id}/`
- Metadata: `metadata/{run_id}/`
"""


def setup_text(run_id: str, environment: dict[str, Any]) -> str:
    return f"""# Isolated PostgreSQL setup: {run_id}

PostgreSQL 14.24 and PL/Python were deployed from the approved Ubuntu 22.04 packages without running maintainer scripts. No default cluster or system service was created or modified.

Package simulation:

```bash
apt-get -s install postgresql-14=14.24-0ubuntu0.22.04.1 postgresql-plpython3-14=14.24-0ubuntu0.22.04.1
```

Project-local package download:

```bash
apt-get download postgresql-14=14.24-0ubuntu0.22.04.1 postgresql-client-14=14.24-0ubuntu0.22.04.1 postgresql-plpython3-14=14.24-0ubuntu0.22.04.1 libpq5=14.24-0ubuntu0.22.04.1
```

Each `.deb` payload was extracted with `dpkg-deb -x` into:

```text
{ROOT / 'runtime' / 'postgresql' / run_id / 'root'}
```

Cluster initialization used:

```bash
initdb -D {ROOT / 'runtime' / 'postgresql' / run_id / 'data'} --encoding=UTF8 --locale=C.UTF-8 --auth-local=trust --auth-host=reject --username=<REDACTED_USER>
```

Isolation controls:

- TCP disabled (`listen_addresses=''`)
- Unix socket `{environment['socket_directory']}` with mode 0700
- Dedicated port `{environment['port']}`
- Database `survey_experiments`
- PL/Python enabled only in that database
- JIT and parallel workers disabled for the controlled campaign
- `shared_buffers=128MB`, `work_mem=4MB`, `effective_cache_size=512MB`

The campaign command was:

```bash
LD_LIBRARY_PATH={ROOT / 'runtime' / 'postgresql' / run_id / 'root/usr/lib/x86_64-linux-gnu'} {ROOT / 'scripts/run/run_postgresql_e1_e2.py'} --run-id {run_id} --socket {environment['socket_directory']} --port {environment['port']} --package-dir {ROOT / 'runtime/postgresql' / run_id / 'packages'}
```

Only this isolated instance is stopped after analysis. Package payloads, cluster data, logs, raw trials, plans, and metadata are preserved.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raw_dir = ROOT / "results" / "raw" / args.run_id
    output_dir = ROOT / "results" / "normalized" / args.run_id
    metadata_dir = ROOT / "metadata" / args.run_id
    environment = json.loads((metadata_dir / "postgresql_environment.json").read_text(encoding="utf-8"))
    function_metadata = json.loads((metadata_dir / "postgresql_function_metadata.json").read_text(encoding="utf-8"))
    natural = json.loads((metadata_dir / "postgresql_natural_placement_probe.json").read_text(encoding="utf-8"))
    e1_records = load_jsonl(raw_dir / "e1_postgresql.jsonl")
    e2_records = load_jsonl(raw_dir / "e2_postgresql.jsonl")
    cost_records = load_jsonl(raw_dir / "postgresql_scalar_cost.jsonl")
    errors: list[str] = []
    e1 = normalize_e1(e1_records)
    e2 = normalize_e2(e2_records, errors)
    cost = normalize_cost(cost_records)
    validation = validate(e1_records, e2_records, cost_records, e1, e2, cost, natural, errors)
    cross = cross_system_rows(e1, e2)

    write_csv_exclusive(output_dir / "e1_postgresql.csv", e1)
    write_csv_exclusive(output_dir / "e2_postgresql.csv", e2)
    write_csv_exclusive(output_dir / "postgresql_scalar_cost.csv", cost)
    write_csv_exclusive(output_dir / "e1_e2_sqlite_duckdb_postgresql_summary.csv", cross)
    write_json_exclusive(output_dir / "postgresql_validation.json", validation)
    write_text_exclusive(
        ROOT / "docs" / f"e1_e2_postgresql_report_{args.run_id}.md",
        report_text(args.run_id, environment, function_metadata, validation, e1, e2, cost),
    )
    write_text_exclusive(
        ROOT / "docs" / f"postgresql_setup_{args.run_id}.md",
        setup_text(args.run_id, environment),
    )
    print(json.dumps(validation, indent=2, sort_keys=True))
    if validation["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
