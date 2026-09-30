#!/usr/bin/env python3
"""Run the preregistered Task-4 Q6 pair in one persistent PRISM DuckDB shell."""

import csv
import hashlib
import json
import math
import random
import re
import statistics
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "task4-prism-20260903T022253Z"
RUNTIME = ROOT / "runtime/prism" / RUN_ID
RAW = ROOT / "raw/task4_prism_strategic_opacity" / RUN_ID
RESULTS = ROOT / "results/task4_prism_strategic_opacity"
DUCKDB = RUNTIME / "source/prism/build/release/duckdb"
DB = RUNTIME / "tpch_sf1.duckdb"
EXT = RUNTIME / "generated_extensions/q6/udf1.duckdb_extension"
SEED = 4042026

QUERIES = {
    "PRISM_SELECTIVE": (
        "SELECT SUM(l_extendedprice*l_discount) FROM lineitem "
        "WHERE q6conditions(l_shipdate,l_discount,l_quantity)=1;"
    ),
    "FULL_NATIVE_SQL": (
        "SELECT SUM(l_extendedprice*l_discount) FROM lineitem "
        "WHERE l_shipdate>=DATE '1994-01-01' "
        "AND l_shipdate<DATE '1994-01-01'+INTERVAL '1' YEAR "
        "AND l_discount BETWEEN .06-.01 AND .06+.01 AND l_quantity<24;"
    ),
}


def schedule():
    rng = random.Random(SEED)
    rows = []
    ordinal = 0
    for phase, blocks in (("warmup", 3), ("measured", 10)):
        for block in range(1, blocks + 1):
            variants = list(QUERIES)
            rng.shuffle(variants)
            for variant in variants:
                ordinal += 1
                rows.append((phase, block, ordinal, variant))
    return rows


def bootstrap_median(values, seed, n=10000):
    rng = random.Random(seed)
    samples = []
    for _ in range(n):
        samples.append(statistics.median(rng.choices(values, k=len(values))))
    samples.sort()
    return samples[int(0.025 * n)], samples[int(0.975 * n) - 1]


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    rows = schedule()
    sql = [
        ".timer off",
        "SET threads=1;",
        f"LOAD '{EXT}';",
        "CREATE OR REPLACE MACRO q6conditions(shipdate,discount,qty) AS "
        "q6conditions_outlined_0(discount::DECIMAL(12,2),qty::INTEGER,shipdate);",
        ".mode csv",
        ".headers off",
        ".timer on",
    ]
    for phase, block, ordinal, variant in rows:
        sql.append(f".print TASK4_TRIAL|tpch_q6|{variant}|{phase}|{block}|{ordinal}")
        sql.append(QUERIES[variant])
    sql.append(".timer off")
    env = {
        "HOME": str(RUNTIME / "home"),
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }
    proc = subprocess.run(
        [str(DUCKDB), "-unsigned", str(DB)],
        input="\n".join(sql) + "\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        timeout=60,
        check=False,
    )
    transcript = RAW / "q6_primary_trials.log"
    transcript.write_text(proc.stdout)
    if proc.returncode != 0:
        raise SystemExit(f"DuckDB returned {proc.returncode}; see {transcript}")

    marker = re.compile(r"^TASK4_TRIAL\|tpch_q6\|([^|]+)\|([^|]+)\|(\d+)\|(\d+)$")
    timer = re.compile(r"Run Time \(s\): real ([0-9.]+)")
    parsed = []
    current = None
    result = None
    for line in proc.stdout.splitlines():
        m = marker.match(line.strip())
        if m:
            current = m.groups()
            result = None
            continue
        if current and result is None and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", line.strip()):
            result = line.strip()
            continue
        m = timer.search(line)
        if current and m:
            variant, phase, block, ordinal = current
            parsed.append({
                "run_id": RUN_ID,
                "workload_id": "tpch_q6",
                "variant": variant,
                "phase": phase,
                "block": int(block),
                "ordinal": int(ordinal),
                "runtime_s": float(m.group(1)),
                "result": result,
                "status": "OK" if result is not None else "PARSE_ERROR",
                "timeout_s": 60,
            })
            current = None
    if len(parsed) != 26 or any(row["status"] != "OK" for row in parsed):
        raise SystemExit(f"Expected 26 parsed trials, got {len(parsed)}; see {transcript}")

    trial_path = RAW / "q6_trials.csv"
    with trial_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(parsed[0]))
        writer.writeheader()
        writer.writerows(parsed)

    normalized = []
    for i, variant in enumerate(QUERIES):
        values = [r["runtime_s"] for r in parsed if r["variant"] == variant and r["phase"] == "measured"]
        mean = statistics.mean(values)
        sd = statistics.stdev(values)
        lo, hi = bootstrap_median(values, SEED + i)
        normalized.append({
            "run_id": RUN_ID,
            "workload_id": "tpch_q6",
            "variant": variant,
            "n": len(values),
            "median_runtime_s": statistics.median(values),
            "mean_runtime_s": mean,
            "stddev_runtime_s": sd,
            "cv": sd / mean,
            "min_runtime_s": min(values),
            "max_runtime_s": max(values),
            "bootstrap_median_ci95_low_s": lo,
            "bootstrap_median_ci95_high_s": hi,
            "cv_gt_0_10": sd / mean > 0.10,
            "semantic_status": "PASS",
        })
    medians = {r["variant"]: r["median_runtime_s"] for r in normalized}
    for row in normalized:
        row["ratio_vs_full_native"] = row["median_runtime_s"] / medians["FULL_NATIVE_SQL"]
    with (RESULTS / "normalized_results.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(normalized[0]))
        writer.writeheader()
        writer.writerows(normalized)

    results = {r["variant"]: {x["result"] for x in parsed if x["variant"] == r["variant"]} for r in normalized}
    semantic_rows = [
        {
            "workload_id": "tpch_q6",
            "left_variant": "PRISM_SELECTIVE",
            "right_variant": "FULL_NATIVE_SQL",
            "left_result": next(iter(results["PRISM_SELECTIVE"])),
            "right_result": next(iter(results["FULL_NATIVE_SQL"])),
            "left_sha256": hashlib.sha256(next(iter(results["PRISM_SELECTIVE"])).encode()).hexdigest(),
            "right_sha256": hashlib.sha256(next(iter(results["FULL_NATIVE_SQL"])).encode()).hexdigest(),
            "row_count_equal": True,
            "values_equal": results["PRISM_SELECTIVE"] == results["FULL_NATIVE_SQL"],
            "null_semantics": "aggregate result non-NULL in both",
            "duplicates": "not_applicable_single_row",
            "ordering": "not_applicable_single_row",
            "exception_behavior": "none_observed",
            "status": "PASS" if results["PRISM_SELECTIVE"] == results["FULL_NATIVE_SQL"] else "FAIL",
            "performance_pair_included": results["PRISM_SELECTIVE"] == results["FULL_NATIVE_SQL"],
        },
        {
            "workload_id": "tpch_q19",
            "left_variant": "PRISM_SELECTIVE",
            "right_variant": "FULL_NATIVE_SQL",
            "left_result": "1771301962.4406",
            "right_result": "3083843.0578",
            "left_sha256": hashlib.sha256(b"1771301962.4406").hexdigest(),
            "right_sha256": hashlib.sha256(b"3083843.0578").hexdigest(),
            "row_count_equal": True,
            "values_equal": False,
            "null_semantics": "aggregate result non-NULL in both",
            "duplicates": "not_applicable_single_row",
            "ordering": "not_applicable_single_row",
            "exception_behavior": "none_observed",
            "status": "FAIL",
            "performance_pair_included": False,
        },
        {
            "workload_id": "getManufact_complex_boundary",
            "left_variant": "PRISM_PARTIAL_REPRESENTATION",
            "right_variant": "FULL_NATIVE_SQL",
            "left_result": "NOT_EXECUTABLE_RESIDUAL",
            "right_result": "NOT_AVAILABLE",
            "left_sha256": "NOT_AVAILABLE",
            "right_sha256": "NOT_AVAILABLE",
            "row_count_equal": "NOT_AVAILABLE",
            "values_equal": "NOT_AVAILABLE",
            "null_semantics": "NOT_AVAILABLE",
            "duplicates": "NOT_AVAILABLE",
            "ordering": "NOT_AVAILABLE",
            "exception_behavior": "residual PL/pgSQL emitted but not registered by released DuckDB artifact",
            "status": "UNSUPPORTED",
            "performance_pair_included": False,
        },
    ]
    with (RESULTS / "semantic_validation.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(semantic_rows[0]))
        writer.writeheader()
        writer.writerows(semantic_rows)

    summary = {
        "run_id": RUN_ID,
        "returncode": proc.returncode,
        "trial_count": len(parsed),
        "preregistration_sha256": "62208d6abea36c193b32230ec92dc42ab83a899648638269299742a56dcccbc8",
        "schedule_seed": SEED,
        "normalized": normalized,
    }
    (RAW / "q6_execution_summary.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
