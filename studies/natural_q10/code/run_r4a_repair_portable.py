#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import random
import sys
from pathlib import Path

BASE = Path(os.environ.get("Q10_STUDY_DIR", Path(__file__).resolve().parents[1])).resolve()
OUT = Path(os.environ.get("STUDY_OUTPUT_DIR", BASE / "r4a_repair")).resolve()

spec = importlib.util.spec_from_file_location("q10_primary", BASE / "code/run_experiment_portable.py")
q = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["q10_primary"] = q
spec.loader.exec_module(q)


def install_exposed(conn):
    with conn.cursor() as cur:
        cur.execute("DROP FUNCTION IF EXISTS q10_probe(date,bpchar)")
        cur.execute("""CREATE FUNCTION q10_probe(date,bpchar) RETURNS boolean
                       LANGUAGE SQL IMMUTABLE PARALLEL SAFE
                       AS $$ SELECT $2='R'::bpchar AND $1>=DATE '1993-10-01'
                                      AND $1<DATE '1994-01-01' $$""")


def install(conn, arm):
    if arm == "R0":
        q.install_variant(conn, "R0", 1.0, 100.0, .01)
    else:
        install_exposed(conn)


def execute(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT q10_reset_metrics()")
        import time
        started = time.perf_counter_ns()
        cur.execute(q.QUERY); rows = cur.fetchall()
        elapsed = (time.perf_counter_ns() - started) / 1e6
    return rows, elapsed, q.metrics(conn)["udf_calls"]


def main():
    for p in (OUT / "logs", OUT / "plans", OUT / "raw", OUT / "normalized"):
        p.mkdir(parents=True, exist_ok=True)
    q.LOGS = OUT / "logs"
    export = q.export_data()
    process, log_handle = q.start_postgres()
    try:
        conn = q.connect(); counts = q.setup_database(conn)
        plans = {}; hashes = {}; gate_calls = {}
        for arm in ("R0", "R4A"):
            install(conn, arm)
            with conn.cursor() as cur:
                cur.execute("EXPLAIN (VERBOSE TRUE, COSTS TRUE, FORMAT JSON) " + q.QUERY)
                plan = cur.fetchone()[0][0]
            rows, _, calls = execute(conn)
            plans[arm] = {"plan": plan, "signature": q.plan_signature(plan)}
            hashes[arm] = q.canonical(rows); gate_calls[arm] = calls
            q.write_json(OUT / "plans" / f"{arm}.json", plans[arm])
        if len(set(hashes.values())) != 1:
            raise RuntimeError("R4a repair semantic mismatch")
        trials = []
        for arm in ("R0", "R4A"):
            install(conn, arm)
            for block in range(3):
                _, elapsed, calls = execute(conn)
                trials.append({"phase": "warmup", "block": block, "position": 0,
                               "arm": arm, "elapsed_ms": elapsed, "udf_calls": calls})
        rng = random.Random(20260928)
        for block in range(10):
            order = ["R0", "R4A"]; rng.shuffle(order)
            for position, arm in enumerate(order):
                install(conn, arm)
                _, elapsed, calls = execute(conn)
                trials.append({"phase": "measured", "block": block, "position": position,
                               "arm": arm, "elapsed_ms": elapsed, "udf_calls": calls})
        conn.close()
    finally:
        q.stop_postgres(process, log_handle)
    q.write_csv(OUT / "raw" / "trials.csv", trials)
    measured = [r for r in trials if r["phase"] == "measured"]
    by = {arm: [r["elapsed_ms"] for r in sorted(measured, key=lambda x: x["block"])
                if r["arm"] == arm] for arm in ("R0", "R4A")}
    result = {"semantic_hash": hashes["R0"], "table_counts": counts,
              "source_sha256": export["source_sha256"], "gate_calls": gate_calls,
              "r0_plan": plans["R0"]["signature"], "r4a_plan": plans["R4A"]["signature"],
              "r4a_inlined": not plans["R4A"]["signature"]["contains_q10_probe"],
              "R0": q.stats.summarize(by["R0"]), "R4A": q.stats.summarize(by["R4A"]),
              "paired_ratio": q.stats.paired_ratio_ci(by["R0"], by["R4A"])}
    q.write_json(OUT / "normalized" / "analysis.json", result)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__": main()
