#!/usr/bin/env python3
"""E5-DC: decision consequence of a collapsed procedural representation.

Executes the preregistered experiment in PREREGISTRATION.md. Data, statistics,
indexes and GUCs are identical across representations; only the value reported
through SupportRequestCost differs. No hints, no forced paths, no disabled path
types.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psycopg2

import datagen
import stats
from datagen import DataSpec

logger = logging.getLogger("e5dc")

STUDY_SOURCE = Path(os.environ.get("STUDY_SOURCE_DIR", Path(__file__).resolve().parents[1])).resolve()
HERE = Path(os.environ.get("STUDY_OUTPUT_DIR", STUDY_SOURCE)).resolve()
RAW = HERE / "raw"
NORMALIZED = HERE / "normalized"
PLANS = HERE / "plans"
LOGS = HERE / "logs"
FIGURES = HERE / "figures"
TABLES = HERE / "tables"
EXTENSION = RAW / "extension/e5dc_support.so"

PG_BIN = Path("/usr/lib/postgresql/14/bin")
PG_SHARE = Path("/usr/share/postgresql/14")
PG_PKGLIB = Path("/usr/lib/postgresql/14/lib")
PG_CONFIG = PG_BIN / "pg_config"
PORT = 55449
DBNAME = "e5dc"

# Preregistered grid (section 8).
K_GRID = [100, 200, 400, 800, 1600]
PRIMARY_K = 400
SEGMENTS = ["COSTLY", "CHEAP"]
MODES = {"M1": "M1_GLOBAL", "M2": "M2_CONDITIONED"}
TIER = "GOLD"

WARMUPS = 3
TRIALS = 10

# Magnitudes frozen by CALIBRATION.md; see PREREGISTRATION.md section 7.
CALIBRATION_SPEC = DataSpec(seed=20260923, fact_rows=800_000, customers=20_000,
                            gold_fraction=0.02, regions=16, costly_fraction=0.05)
CONFIRMATION_SPEC = DataSpec(seed=41, fact_rows=800_000, customers=20_000,
                             gold_fraction=0.02, regions=16, costly_fraction=0.05)
HELDOUT_SCALE_SPEC = DataSpec(seed=41, fact_rows=1_600_000, customers=40_000,
                              gold_fraction=0.02, regions=16, costly_fraction=0.05)


def query_sql(segment: str, regions: int) -> str:
    """The single query under test, frozen by PREREGISTRATION.md section 6."""
    return (
        "SELECT count(*)::bigint, sum(e5_work(f.value, f.loop_count))::numeric "
        "FROM e5_fact f "
        "JOIN e5_customer c ON c.cust_id = f.cust_id "
        f"JOIN e5_region r ON r.region_id = (f.id % {regions}) "
        f"WHERE f.segment = '{segment}' "
        f"AND c.tier = '{TIER}' "
        "AND e5_work(f.value, f.loop_count) >= 0"
    )


def run_text(args: list[str]) -> str:
    return subprocess.run(args, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout.strip()


def connect(socket: Path, database: str = DBNAME):
    connection = psycopg2.connect(host=str(socket), port=PORT, dbname=database,
                                  user="experiment", connect_timeout=5,
                                  application_name="e5-decision-consequence")
    connection.autocommit = True
    return connection


def set_session(conn, representation: str, k: int, spec: DataSpec) -> None:
    """Identical settings for every cell except the representation and K."""
    with conn.cursor() as cursor:
        cursor.execute("SET statement_timeout='600000ms'")
        cursor.execute("SET jit=off")
        cursor.execute("SET max_parallel_workers_per_gather=0")
        cursor.execute("SET enable_seqscan=on")
        cursor.execute("SET enable_indexscan=on")
        cursor.execute("SET enable_bitmapscan=on")
        cursor.execute("SET enable_hashjoin=on")
        cursor.execute("SET enable_mergejoin=on")
        cursor.execute("SET enable_nestloop=on")
        cursor.execute("SET e5dc.mode=%s", (MODES[representation],))
        cursor.execute("SET e5dc.work_units_per_cost_unit=%s", (k,))
        cursor.execute("SET e5dc.units_global=%s", (datagen.global_work_units(spec),))
        cursor.execute("SET e5dc.units_cheap=%s", (datagen.WORK_CHEAP,))
        cursor.execute("SET e5dc.units_costly=%s", (datagen.WORK_COSTLY,))


def walk(node: dict[str, Any]):
    yield node
    for child in node.get("Plans", []) or []:
        yield from walk(child)


def plan_signature(plan: dict[str, Any]) -> dict[str, Any]:
    """Join methods, join order and the node that applies the UDF qual."""
    joins: list[str] = []
    scans: list[str] = []
    udf_nodes: list[str] = []
    for node in walk(plan["Plan"]):
        node_type = node.get("Node Type", "")
        if "Join" in node_type or node_type == "Nested Loop":
            joins.append(node_type)
        if "Scan" in node_type:
            relation = node.get("Relation Name") or node.get("Index Name") or "?"
            scans.append(f"{node_type}:{relation}")
        blob = json.dumps({k: v for k, v in node.items() if k != "Plans"})
        if "e5_work" in blob:
            relation = node.get("Relation Name") or node.get("Index Name") or node_type
            udf_nodes.append(f"{node_type}:{relation}")
    return {
        "join_methods": "|".join(joins),
        "scan_shape": "|".join(scans),
        "udf_applied_at": "|".join(sorted(set(udf_nodes))),
        "signature": "|".join(joins) + "||" + "|".join(scans),
        "total_cost": plan["Plan"].get("Total Cost"),
        "startup_cost": plan["Plan"].get("Startup Cost"),
    }


def capture_plan(conn, segment: str, representation: str, k: int,
                 spec: DataSpec) -> tuple[dict[str, Any], dict[str, Any]]:
    set_session(conn, representation, k, spec)
    with conn.cursor() as cursor:
        cursor.execute("SELECT e5dc_reset_metrics()")
        cursor.execute(f"EXPLAIN (VERBOSE TRUE, COSTS TRUE, FORMAT JSON) "
                       f"{query_sql(segment, spec.regions)}")
        plan = cursor.fetchone()[0][0]
        cursor.execute("SELECT e5dc_metrics()")
        metrics = json.loads(cursor.fetchone()[0])
    return plan, metrics


def execute_once(conn, segment: str, representation: str, k: int,
                 spec: DataSpec) -> dict[str, Any]:
    """One measured execution with exact UDF accounting."""
    set_session(conn, representation, k, spec)
    with conn.cursor() as cursor:
        cursor.execute("SELECT e5dc_reset_metrics()")
        started = time.perf_counter_ns()
        cursor.execute(query_sql(segment, spec.regions))
        rows = cursor.fetchall()
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        cursor.execute("SELECT e5dc_metrics()")
        metrics = json.loads(cursor.fetchone()[0])
    return {
        "elapsed_ms": elapsed_ms,
        "rows": rows,
        "udf_calls": metrics["udf_calls"],
        "udf_work_units": metrics["udf_work_units"],
    }


def setup_database(conn, spec: DataSpec, csv_paths: dict[str, Path]) -> None:
    with conn.cursor() as cursor:
        cursor.execute(f"LOAD '{EXTENSION}'")
        cursor.execute(f"CREATE FUNCTION e5dc_cost_support(internal) RETURNS internal "
                       f"AS '{EXTENSION}', 'e5dc_cost_support' LANGUAGE C STRICT")
        cursor.execute(f"CREATE FUNCTION e5dc_reset_metrics() RETURNS void "
                       f"AS '{EXTENSION}', 'e5dc_reset_metrics' LANGUAGE C STRICT")
        cursor.execute(f"CREATE FUNCTION e5dc_metrics() RETURNS text "
                       f"AS '{EXTENSION}', 'e5dc_metrics' LANGUAGE C STRICT")
        cursor.execute(f"CREATE FUNCTION e5_work(value bigint, loop_count bigint) "
                       f"RETURNS bigint AS '{EXTENSION}', 'e5dc_work' "
                       f"LANGUAGE C IMMUTABLE STRICT PARALLEL UNSAFE "
                       f"SUPPORT e5dc_cost_support COST 100")

        cursor.execute("CREATE TABLE e5_fact (id bigint NOT NULL, cust_id bigint NOT NULL, "
                       "segment text NOT NULL, value bigint NOT NULL, loop_count bigint NOT NULL)")
        cursor.execute("CREATE TABLE e5_customer (cust_id bigint NOT NULL PRIMARY KEY, "
                       "tier text NOT NULL)")
        cursor.execute("CREATE TABLE e5_region (region_id bigint NOT NULL PRIMARY KEY, "
                       "region_name text NOT NULL)")

        for table, key in (("e5_fact", "fact"), ("e5_customer", "customer"), ("e5_region", "region")):
            with csv_paths[key].open("r", encoding="utf-8", newline="") as handle:
                cursor.copy_expert(
                    f"COPY {table} FROM STDIN WITH (FORMAT CSV, HEADER TRUE)", handle)

        # Indexes are identical for both representations.
        cursor.execute("CREATE INDEX e5_fact_cust_idx ON e5_fact (cust_id)")
        cursor.execute("CREATE INDEX e5_fact_segment_idx ON e5_fact (segment)")
        cursor.execute("CREATE INDEX e5_customer_tier_idx ON e5_customer (tier)")
        for table in ("e5_fact", "e5_customer", "e5_region"):
            cursor.execute(f"ANALYZE {table}")


def database_snapshot(conn, spec: DataSpec) -> dict[str, Any]:
    out: dict[str, Any] = {}
    with conn.cursor() as cursor:
        cursor.execute("SELECT version()")
        out["version"] = cursor.fetchone()[0]
        cursor.execute("SHOW cpu_operator_cost")
        out["cpu_operator_cost"] = cursor.fetchone()[0]
        cursor.execute("SELECT segment, count(*), min(loop_count), max(loop_count) "
                       "FROM e5_fact GROUP BY segment ORDER BY segment")
        out["fact_by_segment"] = cursor.fetchall()
        cursor.execute("SELECT tier, count(*) FROM e5_customer GROUP BY tier ORDER BY tier")
        out["customers_by_tier"] = cursor.fetchall()
        cursor.execute("SELECT indexname, indexdef FROM pg_indexes "
                       "WHERE tablename LIKE 'e5%' ORDER BY indexname")
        out["indexes"] = cursor.fetchall()
    out["spec"] = {
        "seed": spec.seed, "fact_rows": spec.fact_rows, "customers": spec.customers,
        "gold_fraction": spec.gold_fraction, "regions": spec.regions,
        "costly_fraction": spec.costly_fraction,
        "units_global_reported_by_M1": datagen.global_work_units(spec),
        "true_work_cheap": datagen.WORK_CHEAP, "true_work_costly": datagen.WORK_COSTLY,
    }
    return out


def start_cluster(tag: str):
    cluster = Path(f"/tmp/e5dc-pg14-{tag}-{os.getpid()}")
    socket = Path(f"/tmp/e5dc-sock-{tag}-{os.getpid()}")
    if cluster.exists() or socket.exists():
        raise RuntimeError("isolated runtime path already exists")
    socket.mkdir(mode=0o700)
    LOGS.mkdir(parents=True, exist_ok=True)
    log_handle = (LOGS / f"postgres-{tag}.log").open("w", encoding="utf-8")
    subprocess.run([str(PG_BIN / "initdb"), "-D", str(cluster), "-L", str(PG_SHARE),
                    "--encoding=UTF8", "--locale=C.UTF-8", "--auth-local=trust",
                    "--auth-host=reject", "--username=experiment"],
                   check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    with (cluster / "postgresql.conf").open("a", encoding="utf-8") as handle:
        handle.write("\n# E5-DC isolated experiment\n")
        handle.write("listen_addresses = ''\n")
        handle.write(f"port = {PORT}\n")
        handle.write(f"unix_socket_directories = '{socket}'\n")
        handle.write("unix_socket_permissions = 0700\n")
        handle.write(f"dynamic_library_path = '{PG_PKGLIB}'\n")
        handle.write("jit = off\nmax_parallel_workers = 0\n")
        handle.write("max_parallel_workers_per_gather = 0\n")
        handle.write("shared_buffers = 1GB\nwork_mem = 256MB\n")
        handle.write("log_min_messages = log\nlog_statement = 'none'\n")
    process = subprocess.Popen([str(PG_BIN / "postgres"), "-D", str(cluster)],
                               stdout=log_handle, stderr=subprocess.STDOUT, text=True)
    for _ in range(300):
        if process.poll() is not None:
            raise RuntimeError("PostgreSQL exited during startup")
        try:
            connect(socket, "postgres").close()
            break
        except psycopg2.OperationalError:
            time.sleep(0.1)
    else:
        raise RuntimeError("PostgreSQL startup timed out")
    admin = connect(socket, "postgres")
    with admin.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE {DBNAME}")
    admin.close()
    return cluster, socket, process, log_handle


def stop_cluster(cluster: Path, socket: Path, process, log_handle) -> None:
    subprocess.run([str(PG_BIN / "pg_ctl"), "-D", str(cluster), "-m", "fast", "stop"],
                   check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.terminate()
    log_handle.close()
    shutil.rmtree(cluster, ignore_errors=True)
    shutil.rmtree(socket, ignore_errors=True)


def run_phase(phase: str, spec: DataSpec) -> dict[str, Any]:
    """Plan grid, semantic gate and timing for one dataset."""
    csv_paths = datagen.generate(spec, RAW / "data")
    cluster, socket, process, log_handle = start_cluster(phase)
    planning: list[dict[str, Any]] = []
    semantic: list[dict[str, Any]] = []
    trials: list[dict[str, Any]] = []
    snapshot: dict[str, Any] = {}
    try:
        conn = connect(socket)
        setup_database(conn, spec, csv_paths)
        snapshot = database_snapshot(conn, spec)
        if not str(snapshot["version"]).startswith("PostgreSQL 14.24 "):
            raise RuntimeError(f"wrong PostgreSQL server: {snapshot['version']}")

        PLANS.mkdir(parents=True, exist_ok=True)
        for k in K_GRID:
            for segment in SEGMENTS:
                for representation in MODES:
                    plan, metrics = capture_plan(conn, segment, representation, k, spec)
                    if metrics["support_calls"] < 1:
                        raise RuntimeError(
                            f"SupportRequestCost not called: {phase}/{k}/{segment}/{representation}")
                    sig = plan_signature(plan)
                    (PLANS / f"{phase}_K{k}_{segment}_{representation}.json").write_text(
                        json.dumps(plan, indent=2), encoding="utf-8")
                    planning.append({"phase": phase, "K": k, "segment": segment,
                                     "representation": representation,
                                     "support_calls": metrics["support_calls"],
                                     "reported_units": metrics["units"][
                                         "global" if representation == "M1"
                                         else segment.lower()],
                                     **sig})
                    logger.info("plan %s K=%d %s %s -> %s", phase, k, segment,
                                representation, sig["join_methods"] or "no-join")

        # Semantic gate: M1 and M2 must agree exactly before any timing is used.
        for k in K_GRID:
            for segment in SEGMENTS:
                results = {}
                for representation in MODES:
                    outcome = execute_once(conn, segment, representation, k, spec)
                    results[representation] = outcome
                    semantic.append({"phase": phase, "K": k, "segment": segment,
                                     "representation": representation,
                                     "result": json.dumps(outcome["rows"], default=str),
                                     "udf_calls": outcome["udf_calls"],
                                     "udf_work_units": outcome["udf_work_units"]})
                if results["M1"]["rows"] != results["M2"]["rows"]:
                    raise RuntimeError(f"semantic mismatch at {phase}/{k}/{segment}")

        # Randomized complete blocks.
        cells = [(k, s, r) for k in K_GRID for s in SEGMENTS for r in MODES]
        for block in range(WARMUPS + TRIALS):
            for k, segment, representation in cells:
                outcome = execute_once(conn, segment, representation, k, spec)
                trials.append({"phase": phase, "block": block, "K": k, "segment": segment,
                               "representation": representation,
                               "elapsed_ms": outcome["elapsed_ms"],
                               "udf_calls": outcome["udf_calls"],
                               "udf_work_units": outcome["udf_work_units"],
                               "retained": block >= WARMUPS})
            logger.info("timing block %d/%d complete (%s)", block + 1, WARMUPS + TRIALS, phase)
        conn.close()
    finally:
        stop_cluster(cluster, socket, process, log_handle)
    return {"planning": planning, "semantic": semantic, "trials": trials,
            "snapshot": snapshot, "spec": spec}


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    for directory in (RAW, NORMALIZED, PLANS, LOGS, FIGURES, TABLES):
        directory.mkdir(parents=True, exist_ok=True)

    environment = {
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "postgres": run_text([str(PG_CONFIG), "--version"]),
        "gcc": run_text(["gcc", "--version"]).splitlines()[0],
        "kernel": run_text(["uname", "-a"]),
        "cpu": platform.processor() or "unknown",
        "preregistration_sha256": (STUDY_SOURCE / "protocol/PREREGISTRATION.sha256").read_text(
            encoding="utf-8").split()[0],
    }

    phases = {}
    for name, spec in (("calibration", CALIBRATION_SPEC),
                       ("confirmation", CONFIRMATION_SPEC),
                       ("heldout_scale", HELDOUT_SCALE_SPEC)):
        logger.info("=== phase %s (seed=%d, fact_rows=%d) ===", name, spec.seed, spec.fact_rows)
        phases[name] = run_phase(name, spec)
        write_csv(NORMALIZED / f"{name}_planning.csv", phases[name]["planning"])
        write_csv(NORMALIZED / f"{name}_semantic.csv", phases[name]["semantic"])
        write_csv(RAW / f"{name}_trials.csv", phases[name]["trials"])
        (HERE / f"environment_{name}.json").write_text(
            json.dumps(phases[name]["snapshot"], indent=2, default=str), encoding="utf-8")

    (HERE / "environment.json").write_text(json.dumps(environment, indent=2), encoding="utf-8")

    # Per-cell summary statistics over retained trials only.
    summary_rows: list[dict[str, Any]] = []
    for name, payload in phases.items():
        cells: dict[tuple[int, str, str], list[float]] = {}
        calls: dict[tuple[int, str, str], int] = {}
        for row in payload["trials"]:
            if not row["retained"]:
                continue
            key = (row["K"], row["segment"], row["representation"])
            cells.setdefault(key, []).append(row["elapsed_ms"])
            calls[key] = row["udf_calls"]
        plan_lookup = {(r["K"], r["segment"], r["representation"]): r
                       for r in payload["planning"]}
        for (k, segment, representation), values in sorted(cells.items()):
            summary = stats.summarize(values, stats.BOOTSTRAP_SEED)
            plan = plan_lookup[(k, segment, representation)]
            summary_rows.append({"phase": name, "K": k, "segment": segment,
                                 "representation": representation,
                                 "udf_calls": calls[(k, segment, representation)],
                                 "join_methods": plan["join_methods"],
                                 "scan_shape": plan["scan_shape"],
                                 "plan_total_cost": plan["total_cost"],
                                 "reported_units": plan["reported_units"], **summary})
    write_csv(NORMALIZED / "e5dc_cell_summary.csv", summary_rows)
    logger.info("wrote %d summary cells", len(summary_rows))


if __name__ == "__main__":
    main()
