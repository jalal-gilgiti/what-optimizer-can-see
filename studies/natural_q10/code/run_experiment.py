#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import os
import random
import shutil
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

import psycopg2

import stats

ROOT = Path(os.environ["SURVEY_ROOT"])
OUT = ROOT / "reviewer_strengthening/11_natural_benchmark_q10"
RAW = OUT / "raw"
NORMALIZED = OUT / "normalized"
PLANS = OUT / "plans"
LOGS = OUT / "logs"
SOURCE_DB = ROOT / "experiments/runtime/prism/task4-prism-20260903T022253Z/tpch_sf1.duckdb"
DUCKDB = ROOT / "experiments/runtime/prism/task4-prism-20260903T022253Z/source/prism/build/release/duckdb"
PG_ROOT = ROOT / "experiments/runtime/postgresql/postgresql-20260829T084826Z/root"
PG_BIN = PG_ROOT / "usr/lib/postgresql/14/bin"
PG_LIB = PG_ROOT / "usr/lib/x86_64-linux-gnu"
PG_SHARE = PG_ROOT / "usr/share/postgresql/14"
EXTENSION = OUT / "raw/extension/q10_support.so"
PG_DATA = Path("/tmp/q10dc-pgdata")
PG_SOCKET = Path("/tmp/q10dc-socket")
DATA = Path("/tmp/q10dc-data")
PORT = 55462
USER = "experiment"
DB = "q10dc"
SEED = 20260928
WARMUPS = 3
BLOCKS = 10
K_GRID = (0.25, 0.5, 1.0, 2.0, 4.0)

QUERY = """
SELECT c.c_custkey, c.c_name,
       SUM(discount_price(l.l_extendedprice, l.l_discount)) AS revenue,
       c.c_acctbal, n.n_name, c.c_address, c.c_phone, c.c_comment
FROM customer c, orders o, lineitem l, nation n
WHERE c.c_custkey = o.o_custkey
  AND l.l_orderkey = o.o_orderkey
  AND q10_probe(o.o_orderdate, l.l_returnflag)
  AND c.c_nationkey = n.n_nationkey
GROUP BY c.c_custkey, c.c_name, c.c_acctbal, c.c_phone,
         n.n_name, c.c_address, c.c_comment
ORDER BY revenue DESC
LIMIT 20
""".strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def export_data() -> dict[str, Any]:
    if DATA.exists():
        shutil.rmtree(DATA)
    DATA.mkdir(parents=True)
    projections = {
        "customer": "c_custkey,c_name,c_acctbal,c_nationkey,c_address,c_phone,c_comment",
        "orders": "o_orderkey,o_custkey,o_orderdate",
        "lineitem": "l_orderkey,l_extendedprice,l_discount,l_returnflag",
        "nation": "n_nationkey,n_name",
    }
    started = time.perf_counter()
    for table, columns in projections.items():
        target = DATA / f"{table}.csv"
        sql = f"COPY (SELECT {columns} FROM {table}) TO '{target}' (FORMAT CSV, HEADER FALSE)"
        subprocess.run(["/lib64/ld-linux-x86-64.so.2", str(DUCKDB), "-readonly", str(SOURCE_DB), "-c", sql],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return {
        "source_database": str(SOURCE_DB.relative_to(ROOT)),
        "source_sha256": sha256(SOURCE_DB),
        "export_seconds": time.perf_counter() - started,
        "exports": {p.name: {"bytes": p.stat().st_size, "sha256": sha256(p)} for p in sorted(DATA.glob("*.csv"))},
    }


def pg_env() -> dict[str, str]:
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = str(PG_LIB) + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    return env


def start_postgres() -> tuple[subprocess.Popen, Any]:
    for path in (PG_DATA, PG_SOCKET):
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
    subprocess.run([str(PG_BIN / "initdb"), "-D", str(PG_DATA), "-L", str(PG_SHARE),
                    "--encoding=UTF8", "--locale=C.UTF-8", "--auth-local=trust",
                    "--auth-host=reject", f"--username={USER}"], check=True, env=pg_env(),
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    with (PG_DATA / "postgresql.conf").open("a", encoding="utf-8") as handle:
        handle.write(f"\nlisten_addresses=''\nport={PORT}\nunix_socket_directories='{PG_SOCKET}'\n")
        handle.write("unix_socket_permissions=0700\njit=off\nmax_parallel_workers_per_gather=0\n")
        handle.write("shared_buffers='1GB'\nwork_mem='256MB'\nmaintenance_work_mem='512MB'\n")
        handle.write("random_page_cost=4.0\neffective_cache_size='4GB'\n")
    log_handle = (LOGS / "postgres.log").open("w", encoding="utf-8")
    process = subprocess.Popen([str(PG_BIN / "postgres"), "-D", str(PG_DATA)], env=pg_env(),
                               stdout=log_handle, stderr=subprocess.STDOUT, text=True)
    for _ in range(300):
        if process.poll() is not None:
            raise RuntimeError("PostgreSQL exited during startup")
        try:
            conn = psycopg2.connect(host=str(PG_SOCKET), port=PORT, dbname="postgres", user=USER, connect_timeout=1)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f"CREATE DATABASE {DB}")
            conn.close()
            break
        except psycopg2.OperationalError:
            time.sleep(0.1)
    else:
        raise RuntimeError("PostgreSQL did not start")
    return process, log_handle


def connect():
    conn = psycopg2.connect(host=str(PG_SOCKET), port=PORT, dbname=DB, user=USER)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout='900s'")
        cur.execute("SET jit=off")
        cur.execute("SET max_parallel_workers_per_gather=0")
    return conn


def stop_postgres(process: subprocess.Popen, log_handle: Any) -> None:
    subprocess.run([str(PG_BIN / "pg_ctl"), "-D", str(PG_DATA), "-m", "fast", "stop"],
                   env=pg_env(), check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    process.wait(timeout=30)
    log_handle.close()


def setup_database(conn) -> dict[str, int]:
    schema = """
    CREATE TABLE customer(c_custkey bigint PRIMARY KEY, c_name text, c_acctbal numeric(15,2),
      c_nationkey integer, c_address text, c_phone text, c_comment text);
    CREATE TABLE orders(o_orderkey bigint PRIMARY KEY, o_custkey bigint, o_orderdate date);
    CREATE TABLE lineitem(l_orderkey bigint, l_extendedprice numeric(15,2),
      l_discount numeric(15,2), l_returnflag char(1));
    CREATE TABLE nation(n_nationkey integer PRIMARY KEY, n_name text);
    """
    with conn.cursor() as cur:
        cur.execute(schema)
        for table in ("customer", "orders", "lineitem", "nation"):
            with (DATA / f"{table}.csv").open("r", encoding="utf-8") as handle:
                cur.copy_expert(f"COPY {table} FROM STDIN WITH (FORMAT CSV)", handle)
        cur.execute("CREATE INDEX orders_cust_idx ON orders(o_custkey)")
        cur.execute("CREATE INDEX lineitem_order_idx ON lineitem(l_orderkey)")
        cur.execute("CREATE INDEX customer_nation_idx ON customer(c_nationkey)")
        cur.execute("ANALYZE")
        cur.execute("SELECT 'customer',count(*) FROM customer UNION ALL SELECT 'orders',count(*) FROM orders UNION ALL SELECT 'lineitem',count(*) FROM lineitem UNION ALL SELECT 'nation',count(*) FROM nation")
        counts = {name: count for name, count in cur.fetchall()}
        cur.execute(f"LOAD '{EXTENSION}'")
        cur.execute(f"CREATE FUNCTION q10_support(internal) RETURNS internal AS '{EXTENSION}','q10_support' LANGUAGE C")
        cur.execute(f"CREATE FUNCTION q10_reset_metrics() RETURNS void AS '{EXTENSION}','q10_reset_metrics' LANGUAGE C")
        cur.execute(f"CREATE FUNCTION q10_metrics() RETURNS text AS '{EXTENSION}','q10_metrics' LANGUAGE C")
        cur.execute("CREATE FUNCTION discount_price(numeric,numeric) RETURNS numeric LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $$ SELECT $1*(1-$2) $$")
    install_variant(conn, "R0", 1.0, 100.0, 0.01)
    return counts


def install_variant(conn, variant: str, k: float, sampled_cost: float, sampled_sel: float) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP FUNCTION IF EXISTS q10_probe(date,bpchar)")
        if variant == "R4A":
            cur.execute("""CREATE FUNCTION q10_probe(date,bpchar) RETURNS boolean
                           LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE
                           AS $$ SELECT $2='R'::bpchar AND $1>=DATE '1993-10-01' AND $1<DATE '1994-01-01' $$""")
        else:
            mode = {"R0": "R0_OPAQUE", "R1": "R1_SCALAR", "R2": "R2_SAMPLED"}[variant]
            procost = 100.0 if variant == "R0" else max(0.000001, sampled_cost * k)
            cur.execute(f"""CREATE FUNCTION q10_probe(date,bpchar) RETURNS boolean
                           AS '{EXTENSION}','q10_probe' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE
                           COST {procost:.9f} SUPPORT q10_support""")
            cur.execute("SELECT set_config('q10dc.mode',%s,false)", (mode,))
            cur.execute("SELECT set_config('q10dc.sampled_procost',%s,false)", (str(sampled_cost),))
            cur.execute("SELECT set_config('q10dc.sampled_selectivity',%s,false)", (str(sampled_sel),))
            cur.execute("SELECT set_config('q10dc.mapping_multiplier',%s,false)", (str(k),))


def metrics(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT q10_metrics()")
        return json.loads(cur.fetchone()[0])


def canonical(rows: list[tuple[Any, ...]]) -> str:
    payload = json.dumps(sorted([[str(x) if x is not None else None for x in row] for row in rows]), separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def acquire_estimate(conn) -> dict[str, Any]:
    install_variant(conn, "R0", 1.0, 100.0, 0.01)
    started = time.perf_counter_ns()
    with conn.cursor() as cur:
        cur.execute("""CREATE TEMP TABLE q10_sample AS
                       SELECT o.o_orderdate,l.l_returnflag
                       FROM lineitem l TABLESAMPLE SYSTEM (1) REPEATABLE (20260928)
                       JOIN orders o ON o.o_orderkey=l.l_orderkey
                       LIMIT 10000""")
        cur.execute("SELECT count(*) FROM q10_sample")
        sample_rows = cur.fetchone()[0]
        cur.execute("SELECT q10_reset_metrics()")
        profile_started = time.perf_counter_ns()
        cur.execute("SELECT count(*) FROM q10_sample WHERE q10_probe(o_orderdate,l_returnflag)")
        matched = cur.fetchone()[0]
        udf_ms = (time.perf_counter_ns() - profile_started) / 1e6
        call_count = metrics(conn)["udf_calls"]
        native_started = time.perf_counter_ns()
        cur.execute("SELECT count(*) FROM q10_sample WHERE l_returnflag='R' AND o_orderdate>=DATE '1993-10-01' AND o_orderdate<DATE '1994-01-01'")
        native_matched = cur.fetchone()[0]
        native_ms = (time.perf_counter_ns() - native_started) / 1e6
        cur.execute("SHOW cpu_operator_cost")
        cpu_operator_cost = float(cur.fetchone()[0])
    acquisition_ms = (time.perf_counter_ns() - started) / 1e6
    incremental_ms = max(0.000001, udf_ms - native_ms)
    per_call_ms = incremental_ms / max(call_count, 1)
    native_per_call_ms = max(native_ms / max(sample_rows, 1), 0.000000001)
    # CREATE FUNCTION COST is expressed in units of cpu_operator_cost, so the
    # measured latency ratio is the procost multiplier; the support hook later
    # converts it back to absolute planner cost by multiplying once.
    procost = max(0.000001, per_call_ms / native_per_call_ms)
    return {
        "sample_rows": sample_rows,
        "matched_rows": matched,
        "native_matched_rows": native_matched,
        "sample_selectivity": matched / sample_rows,
        "udf_profile_ms": udf_ms,
        "native_profile_ms": native_ms,
        "incremental_per_call_ms": per_call_ms,
        "cpu_operator_cost": cpu_operator_cost,
        "sampled_procost": procost,
        "acquisition_ms": acquisition_ms,
    }


def cell_id(variant: str, k: float | None) -> str:
    return variant if k is None else f"{variant}_K{k:g}"


def cells() -> list[tuple[str, float | None]]:
    return [("R0", None), ("R4A", None)] + [(v, k) for v in ("R1", "R2") for k in K_GRID]


def plan_signature(plan: dict[str, Any]) -> dict[str, Any]:
    nodes: list[str] = []
    relations: list[str] = []
    filters: list[str] = []
    def walk(node: dict[str, Any]) -> None:
        nodes.append(node.get("Node Type", ""))
        if node.get("Relation Name"):
            relations.append(node["Relation Name"])
        for key in ("Filter", "Join Filter", "Hash Cond", "Index Cond"):
            if node.get(key):
                filters.append(f"{key}:{node[key]}")
        for child in node.get("Plans", []):
            walk(child)
    walk(plan["Plan"])
    return {"nodes_preorder": nodes, "relations_preorder": relations, "conditions": filters,
            "contains_q10_probe": "q10_probe" in json.dumps(plan),
            "signature": hashlib.sha256(json.dumps(plan_signature_core(plan["Plan"]), sort_keys=True).encode()).hexdigest()}


def plan_signature_core(node: dict[str, Any]) -> dict[str, Any]:
    return {"node": node.get("Node Type"), "relation": node.get("Relation Name"),
            "filter": node.get("Filter"), "join_filter": node.get("Join Filter"),
            "hash_cond": node.get("Hash Cond"), "index_cond": node.get("Index Cond"),
            "children": [plan_signature_core(child) for child in node.get("Plans", [])]}


def capture_and_gate(conn, estimate: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for variant, k in cells():
        actual_k = 1.0 if k is None else k
        install_variant(conn, variant, actual_k, estimate["sampled_procost"], estimate["sample_selectivity"])
        with conn.cursor() as cur:
            cur.execute("SELECT q10_reset_metrics()")
            started = time.perf_counter_ns()
            cur.execute("EXPLAIN (VERBOSE TRUE, COSTS TRUE, FORMAT JSON) " + QUERY)
            plan = cur.fetchone()[0][0]
            plan_ms = (time.perf_counter_ns() - started) / 1e6
            support = metrics(conn)
            cur.execute("SELECT q10_reset_metrics()")
            cur.execute(QUERY)
            result = cur.fetchall()
            execution_metrics = metrics(conn)
        ident = cell_id(variant, k)
        write_json(PLANS / f"{ident}.json", {"cell": ident, "plan": plan, "signature": plan_signature(plan),
                                                     "plan_capture_ms": plan_ms, "support_metrics": support})
        rows.append({"cell": ident, "variant": variant, "K_B": "" if k is None else k,
                     "result_hash": canonical(result), "result_rows": len(result),
                     "gate_udf_calls": execution_metrics["udf_calls"], "plan_capture_ms": plan_ms,
                     "support_elapsed_ns": support["support_elapsed_ns"], **plan_signature(plan)})
    hashes = {row["result_hash"] for row in rows}
    if len(hashes) != 1:
        raise RuntimeError(f"semantic gate failed: {hashes}")
    return rows


def time_query(conn) -> tuple[float, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT q10_reset_metrics()")
        started = time.perf_counter_ns()
        cur.execute(QUERY)
        cur.fetchall()
        elapsed_ms = (time.perf_counter_ns() - started) / 1e6
    return elapsed_ms, metrics(conn)["udf_calls"]


def run_trials(conn, estimate: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for variant, k in cells():
        install_variant(conn, variant, 1.0 if k is None else k, estimate["sampled_procost"], estimate["sample_selectivity"])
        for warmup in range(WARMUPS):
            elapsed, calls = time_query(conn)
            output.append({"cell": cell_id(variant, k), "variant": variant, "K_B": "" if k is None else k,
                           "phase": "warmup", "block": warmup, "elapsed_ms": elapsed, "udf_calls": calls})
    rng = random.Random(SEED)
    fixed = cells()
    for block in range(BLOCKS):
        order = fixed[:]
        rng.shuffle(order)
        for position, (variant, k) in enumerate(order):
            install_variant(conn, variant, 1.0 if k is None else k, estimate["sampled_procost"], estimate["sample_selectivity"])
            elapsed, calls = time_query(conn)
            output.append({"cell": cell_id(variant, k), "variant": variant, "K_B": "" if k is None else k,
                           "phase": "measured", "block": block, "position": position,
                           "elapsed_ms": elapsed, "udf_calls": calls})
    return output


def analyze(trials: list[dict[str, Any]], gates: list[dict[str, Any]], estimate: dict[str, Any]) -> dict[str, Any]:
    measured = [row for row in trials if row["phase"] == "measured"]
    summaries: list[dict[str, Any]] = []
    by_cell: dict[str, list[dict[str, Any]]] = {}
    for row in measured:
        by_cell.setdefault(row["cell"], []).append(row)
    gate_by_cell = {row["cell"]: row for row in gates}
    for ident, values in sorted(by_cell.items()):
        timing = [row["elapsed_ms"] for row in sorted(values, key=lambda x: x["block"])]
        summaries.append({"cell": ident, "variant": values[0]["variant"], "K_B": values[0]["K_B"],
                          "udf_calls": int(statistics.median(row["udf_calls"] for row in values)),
                          "plan_signature": gate_by_cell[ident]["signature"], **stats.summarize(timing)})
    def paired(a: str, b: str) -> dict[str, Any]:
        av = [r["elapsed_ms"] for r in sorted(by_cell[a], key=lambda x: x["block"])]
        bv = [r["elapsed_ms"] for r in sorted(by_cell[b], key=lambda x: x["block"])]
        return {"numerator": a, "denominator": b, **stats.paired_ratio_ci(av, bv)}
    comparisons = [paired("R1_K1", "R2_K1"), paired("R0", "R2_K1"), paired("R0", "R4A")]
    summary_by = {row["cell"]: row for row in summaries}
    primary_ratio = comparisons[0]
    primary = {
        "semantic_identity": len({row["result_hash"] for row in gates}) == 1,
        "plans_differ": gate_by_cell["R1_K1"]["signature"] != gate_by_cell["R2_K1"]["signature"],
        "call_ratio": max(summary_by["R1_K1"]["udf_calls"], summary_by["R2_K1"]["udf_calls"]) /
                      max(1, min(summary_by["R1_K1"]["udf_calls"], summary_by["R2_K1"]["udf_calls"])),
        "runtime_ratio": primary_ratio["ratio"], "runtime_ratio_ci_low": primary_ratio["ci_low"],
        "runtime_ratio_ci_high": primary_ratio["ci_high"],
        "both_cv_le_010": summary_by["R1_K1"]["cv"] <= 0.10 and summary_by["R2_K1"]["cv"] <= 0.10,
    }
    savings = summary_by["R1_K1"]["median_ms"] - summary_by["R2_K1"]["median_ms"]
    primary["acquisition_amortization_executions"] = estimate["acquisition_ms"] / savings if savings > 0 else None
    criteria = [primary["semantic_identity"], primary["plans_differ"], primary["call_ratio"] >= 2,
                primary["runtime_ratio"] >= 2 and primary["runtime_ratio_ci_low"] > 1,
                primary["both_cv_le_010"],
                primary["acquisition_amortization_executions"] is not None and primary["acquisition_amortization_executions"] <= 10]
    primary["criteria"] = criteria
    primary["verdict"] = "SUPPORTED" if all(criteria) else ("NOT_SUPPORTED" if not all(criteria[:2]) else "PARTIAL")
    return {"summaries": summaries, "paired_comparisons": comparisons, "primary": primary}


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    for directory in (RAW, NORMALIZED, PLANS, LOGS, EXTENSION.parent):
        directory.mkdir(parents=True, exist_ok=True)
    export = export_data()
    process, log_handle = start_postgres()
    try:
        conn = connect()
        load_started = time.perf_counter()
        counts = setup_database(conn)
        load_seconds = time.perf_counter() - load_started
        estimate = acquire_estimate(conn)
        gates = capture_and_gate(conn, estimate)
        trials = run_trials(conn, estimate)
        analysis = analyze(trials, gates, estimate)
        conn.close()
    finally:
        stop_postgres(process, log_handle)
    environment = {"postgresql": "14.24", "workload": "PRISM/TPC-H Q10", "scale": "SF1",
                   "seed": SEED, "warmups": WARMUPS, "blocks": BLOCKS, "K_B": K_GRID,
                   "table_counts": counts, "load_seconds": load_seconds, "export": export}
    write_json(OUT / "environment.json", environment)
    write_json(NORMALIZED / "estimator.json", estimate)
    write_csv(RAW / "trials.csv", trials)
    write_csv(NORMALIZED / "semantic_and_plans.csv", gates)
    write_csv(NORMALIZED / "cell_summary.csv", analysis["summaries"])
    write_csv(NORMALIZED / "paired_ratio_ci.csv", analysis["paired_comparisons"])
    write_json(NORMALIZED / "analysis.json", analysis)
    print(json.dumps(analysis["primary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
