#!/usr/bin/env python3
"""E5-DC-S: estimator-error and regime-map sensitivity studies.

S1 — estimator error tolerance. The confirmatory result gives M2 the exact
segment-conditioned work value, which makes it an oracle. Here the reported
conditioned value is deliberately perturbed across a wide error range, so the
accuracy a real estimator would need can be stated rather than assumed.

S2 — regime map. The confirmatory result uses one skew (5% costly rows, 2% of
customers selected). Here skew and join selectivity are swept, so the size and
shape of the region in which the benefit exists is mapped rather than asserted.

Both are descriptive sensitivity studies of an already-confirmed result, not new
confirmatory tests, and are labelled as such.
"""

from __future__ import annotations

import csv
import json
import logging
import sys
import time
from typing import Any

import datagen
import run_experiment as rx
import stats
from datagen import DataSpec

logger = logging.getLogger("e5dc-s")

PRIMARY_K = 400
TRIALS_S1 = 10
TRIALS_S2 = 5
WARMUPS = 3

# Reported COSTLY work units to test. Truth is 1000; the collapsed global mean is
# 60. Values span a 0.06x .. 4x error range around the truth.
REPORTED_UNITS = [60, 125, 250, 400, 500, 625, 750, 900, 1000, 1250, 2000, 4000]

SKEWS = [0.01, 0.05, 0.10, 0.25, 0.50]
GOLD_FRACTIONS = [0.005, 0.02, 0.05, 0.20]


def set_session_units(conn, k: int, spec: DataSpec, reported_costly: int) -> None:
    """M2 wiring with a deliberately perturbed conditioned value."""
    with conn.cursor() as cursor:
        cursor.execute("SET statement_timeout='600000ms'")
        cursor.execute("SET jit=off")
        cursor.execute("SET max_parallel_workers_per_gather=0")
        for guc in ("enable_seqscan", "enable_indexscan", "enable_bitmapscan",
                    "enable_hashjoin", "enable_mergejoin", "enable_nestloop"):
            cursor.execute(f"SET {guc}=on")
        cursor.execute("SET e5dc.mode='M2_CONDITIONED'")
        cursor.execute("SET e5dc.work_units_per_cost_unit=%s", (k,))
        cursor.execute("SET e5dc.units_global=%s", (datagen.global_work_units(spec),))
        cursor.execute("SET e5dc.units_cheap=%s", (datagen.WORK_CHEAP,))
        cursor.execute("SET e5dc.units_costly=%s", (reported_costly,))


def measure(conn, spec: DataSpec, trials: int) -> dict[str, Any]:
    values: list[float] = []
    calls = 0
    for block in range(WARMUPS + trials):
        with conn.cursor() as cursor:
            cursor.execute("SELECT e5dc_reset_metrics()")
            started = time.perf_counter_ns()
            cursor.execute(rx.query_sql("COSTLY", spec.regions))
            rows = cursor.fetchall()
            elapsed = (time.perf_counter_ns() - started) / 1_000_000.0
            cursor.execute("SELECT e5dc_metrics()")
            metrics = json.loads(cursor.fetchone()[0])
        if block >= WARMUPS:
            values.append(elapsed)
        calls = metrics["udf_calls"]
    return {"values": values, "udf_calls": calls, "rows": rows,
            "support_elapsed_ns": metrics["support_elapsed_ns"],
            "support_calls": metrics["support_calls"]}


def plan_for_units(conn, spec: DataSpec, k: int, reported: int) -> dict[str, Any]:
    set_session_units(conn, k, spec, reported)
    with conn.cursor() as cursor:
        cursor.execute("SELECT e5dc_reset_metrics()")
        cursor.execute(f"EXPLAIN (VERBOSE TRUE, COSTS TRUE, FORMAT JSON) "
                       f"{rx.query_sql('COSTLY', spec.regions)}")
        plan = cursor.fetchone()[0][0]
    return rx.plan_signature(plan)


def study_s1() -> list[dict[str, Any]]:
    """Estimator error tolerance on the confirmation dataset."""
    spec = rx.CONFIRMATION_SPEC
    paths = datagen.generate(spec, rx.RAW / "data")
    cluster, sock, proc, lh = rx.start_cluster("s1")
    out: list[dict[str, Any]] = []
    try:
        conn = rx.connect(sock)
        rx.setup_database(conn, spec, paths)
        baseline = None
        for reported in REPORTED_UNITS:
            sig = plan_for_units(conn, spec, PRIMARY_K, reported)
            set_session_units(conn, PRIMARY_K, spec, reported)
            m = measure(conn, spec, TRIALS_S1)
            summary = stats.summarize(m["values"], stats.BOOTSTRAP_SEED)
            if baseline is None:
                baseline = m["udf_calls"]
            out.append({
                "reported_costly_units": reported,
                "true_costly_units": datagen.WORK_COSTLY,
                "relative_error": round(reported / datagen.WORK_COSTLY - 1.0, 4),
                "join_methods": sig["join_methods"],
                "scan_shape": sig["scan_shape"],
                "udf_calls": m["udf_calls"],
                "support_elapsed_ns": m["support_elapsed_ns"],
                "support_calls": m["support_calls"],
                "result": json.dumps(m["rows"], default=str),
                **summary,
            })
            logger.info("S1 reported=%s calls=%s median=%.1fms joins=%s",
                        reported, m["udf_calls"], summary["median_ms"], sig["join_methods"])
        conn.close()
    finally:
        rx.stop_cluster(cluster, sock, proc, lh)
    return out


def study_s2() -> list[dict[str, Any]]:
    """Regime map over segment skew and join selectivity."""
    out: list[dict[str, Any]] = []
    for costly in SKEWS:
        for gold in GOLD_FRACTIONS:
            spec = DataSpec(seed=41, fact_rows=400_000, customers=20_000,
                            gold_fraction=gold, regions=16, costly_fraction=costly)
            paths = datagen.generate(spec, rx.RAW / "regime")
            tag = f"s2c{int(costly*100)}g{int(gold*1000)}"
            cluster, sock, proc, lh = rx.start_cluster(tag)
            try:
                conn = rx.connect(sock)
                rx.setup_database(conn, spec, paths)
                cell: dict[str, Any] = {}
                for rep in ("M1", "M2"):
                    plan, _ = rx.capture_plan(conn, "COSTLY", rep, PRIMARY_K, spec)
                    sig = rx.plan_signature(plan)
                    rx.set_session(conn, rep, PRIMARY_K, spec)
                    m = measure(conn, spec, TRIALS_S2)
                    summary = stats.summarize(m["values"], stats.BOOTSTRAP_SEED)
                    cell[rep] = {"sig": sig, "calls": m["udf_calls"],
                                 "rows": m["rows"], **summary}
                same_result = cell["M1"]["rows"] == cell["M2"]["rows"]
                c1, c2 = cell["M1"]["calls"], cell["M2"]["calls"]
                t1, t2 = cell["M1"]["median_ms"], cell["M2"]["median_ms"]
                out.append({
                    "costly_fraction": costly, "gold_fraction": gold,
                    "K": PRIMARY_K,
                    "m1_reported_units": datagen.global_work_units(spec),
                    "true_costly_units": datagen.WORK_COSTLY,
                    "understatement_factor": round(
                        datagen.WORK_COSTLY / datagen.global_work_units(spec), 2),
                    "m1_join_methods": cell["M1"]["sig"]["join_methods"],
                    "m2_join_methods": cell["M2"]["sig"]["join_methods"],
                    "plans_differ": (cell["M1"]["sig"]["signature"]
                                     != cell["M2"]["sig"]["signature"]),
                    "m1_udf_calls": c1, "m2_udf_calls": c2,
                    "udf_call_ratio": round(max(c1, c2) / min(c1, c2), 3),
                    "m1_median_ms": round(t1, 3), "m2_median_ms": round(t2, 3),
                    "runtime_ratio": round(max(t1, t2) / min(t1, t2), 3),
                    "m1_cv": round(cell["M1"]["cv"], 4), "m2_cv": round(cell["M2"]["cv"], 4),
                    "ci_disjoint": stats.intervals_disjoint(cell["M1"], cell["M2"]),
                    "semantically_identical": same_result,
                })
                logger.info("S2 costly=%.2f gold=%.3f callx=%.1f msx=%.2f",
                            costly, gold, out[-1]["udf_call_ratio"],
                            out[-1]["runtime_ratio"])
                conn.close()
            finally:
                rx.stop_cluster(cluster, sock, proc, lh)
    return out


def write_csv(name: str, rows: list[dict[str, Any]]) -> None:
    path = rx.NORMALIZED / name
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    logger.info("wrote %s (%d rows)", path, len(rows))


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    write_csv("e5dc_s1_estimator_error.csv", study_s1())
    write_csv("e5dc_s2_regime_map.csv", study_s2())


if __name__ == "__main__":
    main()
