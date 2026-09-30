#!/usr/bin/env python3
"""Measure EXPLAIN wall time and write Task-4 optimization-overhead evidence."""

import csv
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

QUERIES = {
    "PRISM_SELECTIVE": "EXPLAIN SELECT SUM(l_extendedprice*l_discount) FROM lineitem WHERE q6conditions(l_shipdate,l_discount,l_quantity)=1;",
    "FULL_NATIVE_SQL": "EXPLAIN SELECT SUM(l_extendedprice*l_discount) FROM lineitem WHERE l_shipdate>=DATE '1994-01-01' AND l_shipdate<DATE '1994-01-01'+INTERVAL '1' YEAR AND l_discount BETWEEN .06-.01 AND .06+.01 AND l_quantity<24;",
}


def main():
    rng = random.Random(4042026)
    schedule = []
    sql = [
        ".timer off",
        "SET threads=1;",
        f"LOAD '{EXT}';",
        "CREATE OR REPLACE MACRO q6conditions(shipdate,discount,qty) AS q6conditions_outlined_0(discount::DECIMAL(12,2),qty::INTEGER,shipdate);",
        ".timer on",
    ]
    ordinal = 0
    for block in range(1, 11):
        variants = list(QUERIES)
        rng.shuffle(variants)
        for variant in variants:
            ordinal += 1
            schedule.append((block, ordinal, variant))
            sql.append(f".print TASK4_PLAN|{variant}|{block}|{ordinal}")
            sql.append(QUERIES[variant])
    sql.append(".timer off")
    env = {"HOME": str(RUNTIME / "home"), "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
    proc = subprocess.run(
        [str(DUCKDB), "-unsigned", str(DB)], input="\n".join(sql) + "\n",
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env, timeout=60, check=False,
    )
    (RAW / "q6_plan_timing.log").write_text(proc.stdout)
    if proc.returncode:
        raise SystemExit(proc.returncode)
    marker = re.compile(r"^TASK4_PLAN\|([^|]+)\|(\d+)\|(\d+)$")
    timer = re.compile(r"Run Time \(s\): real ([0-9.]+)")
    current = None
    trials = []
    for line in proc.stdout.splitlines():
        m = marker.match(line.strip())
        if m:
            current = m.groups()
        m = timer.search(line)
        if current and m:
            variant, block, order = current
            trials.append({"run_id": RUN_ID, "workload_id": "tpch_q6", "variant": variant, "block": int(block), "ordinal": int(order), "explain_wall_s": float(m.group(1))})
            current = None
    if len(trials) != 20:
        raise SystemExit(f"expected 20 plan trials, got {len(trials)}")
    with (RAW / "q6_plan_trials.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(trials[0]))
        w.writeheader(); w.writerows(trials)

    med = {}
    for variant in QUERIES:
        med[variant] = statistics.median(x["explain_wall_s"] for x in trials if x["variant"] == variant)
    rows = [
        ("tpch_q6", "PRISM_SELECTIVE", "SSAConstruction", "51", "ms", "q6_transpile_unsigned.log", "AVAILABLE", "artifact per-pass output"),
        ("tpch_q6", "PRISM_SELECTIVE", "InstructionElimination_first", "28", "ms", "q6_transpile_unsigned.log", "AVAILABLE", "artifact per-pass output"),
        ("tpch_q6", "PRISM_SELECTIVE", "QueryMotion", "0", "ms", "q6_transpile_unsigned.log", "AVAILABLE", "artifact per-pass output"),
        ("tpch_q6", "PRISM_SELECTIVE", "Outlining_compile_load", "5548", "ms", "q6_transpile_unsigned.log", "AVAILABLE", "includes generated-extension compile and load"),
        ("tpch_q6", "PRISM_SELECTIVE", "transpile_wall", "6", "s", "script timestamps", "AVAILABLE_COARSE", "one-second timestamp resolution"),
        ("tpch_q6", "PRISM_SELECTIVE", "explain_wall_median", str(med["PRISM_SELECTIVE"]), "s", "q6_plan_trials.csv", "AVAILABLE", "10 EXPLAIN statements in persistent session"),
        ("tpch_q6", "PRISM_SELECTIVE", "physical_plan_nodes", "4", "nodes", "tpch_q6_prism_selective.txt", "AVAILABLE", "aggregate; projection; filter; scan"),
        ("tpch_q6", "FULL_NATIVE_SQL", "prism_transformation", "0", "ms", "not applicable", "NOT_APPLICABLE", "native repository SQL"),
        ("tpch_q6", "FULL_NATIVE_SQL", "explain_wall_median", str(med["FULL_NATIVE_SQL"]), "s", "q6_plan_trials.csv", "AVAILABLE", "10 EXPLAIN statements in persistent session"),
        ("tpch_q6", "FULL_NATIVE_SQL", "physical_plan_nodes", "3", "nodes", "tpch_q6_full_native_sql.txt", "AVAILABLE", "aggregate; projection; filtered scan"),
        ("tpch_q19", "PRISM_SELECTIVE", "SSAConstruction", "60", "ms", "q19_transpile.log", "AVAILABLE", "artifact per-pass output"),
        ("tpch_q19", "PRISM_SELECTIVE", "InstructionElimination_first", "20", "ms", "q19_transpile.log", "AVAILABLE", "artifact per-pass output"),
        ("tpch_q19", "PRISM_SELECTIVE", "Outlining_compile_load", "5461", "ms", "q19_transpile.log", "AVAILABLE", "pair excluded after semantic failure"),
        ("tpch_q19", "PRISM_SELECTIVE", "physical_plan_nodes", "8", "nodes", "tpch_q19_prism_selective.txt", "AVAILABLE", "captured before semantic exclusion"),
        ("tpch_q19", "FULL_NATIVE_SQL", "physical_plan_nodes", "9", "nodes", "tpch_q19_full_native_sql.txt", "AVAILABLE", "captured before semantic exclusion"),
        ("getManufact_complex_boundary", "PRISM_PARTIAL_REPRESENTATION", "QueryMotion", "6", "ms", "getManufact_complex_transpile.log", "AVAILABLE", "artifact per-pass output"),
        ("getManufact_complex_boundary", "PRISM_PARTIAL_REPRESENTATION", "Outlining_compile_load", "10447", "ms", "getManufact_complex_transpile.log", "AVAILABLE", "two outlined C++ functions compiled/loaded"),
        ("getManufact_complex_boundary", "PRISM_PARTIAL_REPRESENTATION", "memo_search_space", "NOT_AVAILABLE", "", "artifact", "NOT_AVAILABLE", "not exposed by released artifact"),
        ("all", "all", "parsing_time", "NOT_AVAILABLE", "", "artifact", "NOT_AVAILABLE", "not separately instrumented"),
        ("all", "all", "binding_time", "NOT_AVAILABLE", "", "artifact", "NOT_AVAILABLE", "not separately instrumented"),
        ("all", "all", "peak_memory", "NOT_AVAILABLE", "", "artifact", "NOT_AVAILABLE", "not directly reported"),
    ]
    fields = ["workload_id", "variant", "metric", "value", "unit", "source", "availability", "note"]
    with (RESULTS / "optimization_overhead.csv").open("w", newline="") as f:
        w = csv.writer(f); w.writerow(fields); w.writerows(rows)


if __name__ == "__main__":
    main()
