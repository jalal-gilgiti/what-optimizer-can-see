#!/usr/bin/env python3
"""Execute the preregistered Task 2 UDFBench tiny-scale validation.

The runner uses only the seven workloads frozen in the Task 2 preregistration.
It preserves released SQL/UDF source semantics, creates new Task 2 databases,
captures plans before timing, and writes every warmup and measured trial.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import inspect
import json
import math
import os
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import duckdb
import numpy as np
import psycopg2


ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = ROOT / "runtime" / "task2_udfbench_validation" / "UDFBench"
RUNTIME = ROOT / "runtime" / "task2_udfbench_validation"
RAW = ROOT / "raw" / "task2_udfbench_validation"
PLANS = ROOT / "plans" / "task2_udfbench_validation"
METADATA = ROOT / "metadata" / "task2_udfbench_validation"
PREREG = METADATA / "preregistration.json"
COMMIT = "8da987566590e2534f24cd97bcfaa9157c24962c"
SELECTED = ("Q1", "Q7", "Q8", "Q9", "Q10", "Q14", "Q17")
SQLITE_SUPPORTED = {"Q1", "Q8", "Q14"}
DUCKDB_SUPPORTED = set(SELECTED)
POSTGRES_SUPPORTED = set(SELECTED)
WARMUPS = 3
REPETITIONS = 10
SEED = 20260902
TIMEOUT_SECONDS = 60
PG_PORT = 55439
PG_USER = "<REDACTED_USER>"
PG_DATABASE = "udfbench_task2"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def canonical_rows(rows: list[tuple[Any, ...]], workload: str) -> dict[str, Any]:
    normalized = [[normalize_value(value) for value in row] for row in rows]
    ordered = sorted(normalized, key=stable_json)
    payload = stable_json(ordered)
    invariant = ordered
    if workload == "Q10":
        # Cluster labels may vary. Preserve the semantic data-point identity/value invariant.
        invariant = sorted([row[1:] if len(row) >= 4 else row for row in normalized], key=stable_json)
    return {
        "row_count": len(rows),
        "output_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "semantic_invariant_sha256": hashlib.sha256(stable_json(invariant).encode("utf-8")).hexdigest(),
        "sample": ordered[:10],
    }


def normalize_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return round(value, 10)
    if isinstance(value, (list, tuple)):
        return [normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): normalize_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    return str(value)


class Timeout(Exception):
    pass


@contextmanager
def deadline(seconds: int):
    def handler(signum: int, frame: Any) -> None:
        raise Timeout(f"exceeded {seconds}s")

    previous = signal.signal(signal.SIGALRM, handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def load_module(path: Path):
    name = "task2_" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_sql(engine: str, workload: str) -> str:
    sql = (UPSTREAM / "engines" / engine / "queries" / f"{workload.lower()}.sql").read_text(encoding="utf-8").strip()
    if engine == "postgres":
        sql = re.sub(r"^\s*explain\s*\(analyse\s*,\s*buffers\)\s*", "", sql, flags=re.I)
        sql = re.sub(r"^\s*explain\s*\(analyse,buffers\)\s*", "", sql, flags=re.I)
        external = (UPSTREAM / "dataset" / "files" / "tiny").resolve()
        for filename in ("pubmed_q7.txt", "crossref.txt", "crossref.xml", "arxiv.csv", "pubmed.txt", "data.txt"):
            sql = sql.replace(f"'{filename}'", "'" + str(external / filename).replace("'", "''") + "'")
    return sql.rstrip(";")


def write_json_new(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")


def log_event(handle, **payload: Any) -> None:
    handle.write(stable_json({"timestamp_ns": time.time_ns(), **payload}) + "\n")
    handle.flush()


def setup_duckdb(path: Path) -> dict[str, int]:
    if path.exists():
        raise RuntimeError(f"refusing to overwrite {path}")
    connection = duckdb.connect(str(path))
    connection.execute("PRAGMA threads=1")
    connection.execute((UPSTREAM / "engines/duckdb/scripts/duckdb_schema.sql").read_text(encoding="utf-8"))
    csv_root = (UPSTREAM / "dataset/csvs/tiny").resolve()
    tables = [
        "artifacts", "artifact_abstracts", "artifact_authorlists", "artifact_authors",
        "artifact_charges", "artifact_citations", "projects", "projects_artifacts",
        "project_artifactcount", "views_stats",
    ]
    counts: dict[str, int] = {}
    for table in tables:
        source = str(csv_root / f"{table}.csv").replace("'", "''")
        connection.execute(f"COPY {table} FROM '{source}' (FORMAT CSV, QUOTE '\"', DELIMITER ',', HEADER FALSE)")
        counts[table] = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    connection.execute("CHECKPOINT")
    connection.close()
    return counts


def register_duckdb(connection):
    engine_root = UPSTREAM / "engines" / "duckdb"
    sys.path.insert(0, str(engine_root))
    from udfs.scalar import Scalar  # type: ignore
    from udfs.aggregate import Aggregate  # type: ignore
    from udfs.table import Table  # type: ignore

    exec_module = load_module(engine_root / "queries" / "exec.py")
    scalar = Scalar()
    aggregate = Aggregate(connection)
    table = Table(connection, str((UPSTREAM / "dataset/files/tiny").resolve()))
    if not exec_module.createfunctions(connection, scalar, aggregate, table):
        raise RuntimeError("released DuckDB UDF registration returned false")


def sqlite_aggregate_adapter(source_class):
    class Adapter(source_class):
        def finalize(self):
            return self.final()
    Adapter.__name__ = source_class.__name__
    return Adapter


def register_sqlite(connection) -> None:
    base = UPSTREAM / "engines/sqlite/udfs"
    for path in sorted((base / "scalar").glob("*.py")):
        module = load_module(path)
        for name, obj in inspect.getmembers(module, inspect.isfunction):
            if getattr(obj, "registered", False):
                connection.create_function(name, len(inspect.signature(obj).parameters), obj, deterministic=True)
    for path in sorted((base / "aggregate").glob("*.py")):
        module = load_module(path)
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if getattr(obj, "registered", False):
                arity = len(inspect.signature(obj.step).parameters) - 1
                connection.create_aggregate(name, arity, sqlite_aggregate_adapter(obj))


def setup_sqlite(path: Path) -> dict[str, int]:
    import sqlite3

    if path.exists():
        raise RuntimeError(f"refusing to overwrite {path}")
    connection = sqlite3.connect(path)
    connection.executescript((UPSTREAM / "engines/sqlite/scripts/sqlite_schema.sql").read_text(encoding="utf-8"))
    csv_root = UPSTREAM / "dataset/csvs/tiny"
    tables = [
        "artifacts", "artifact_abstracts", "artifact_authorlists", "artifact_authors",
        "artifact_charges", "artifact_citations", "projects", "projects_artifacts",
        "project_artifactcount", "views_stats",
    ]
    counts: dict[str, int] = {}
    for table in tables:
        width = connection.execute(f"PRAGMA table_info({table})").fetchall()
        placeholders = ",".join("?" for _ in width)
        with (csv_root / f"{table}.csv").open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            batch = []
            for row in reader:
                if len(row) != len(width):
                    raise RuntimeError(f"{table}: CSV width {len(row)} != schema width {len(width)}")
                batch.append(tuple(None if value == "" else value for value in row))
                if len(batch) >= 5000:
                    connection.executemany(f"INSERT INTO {table} VALUES ({placeholders})", batch)
                    batch.clear()
            if batch:
                connection.executemany(f"INSERT INTO {table} VALUES ({placeholders})", batch)
        counts[table] = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    connection.commit()
    connection.execute("ANALYZE")
    connection.commit()
    connection.close()
    return counts


def pg_paths() -> dict[str, Path]:
    package_root = ROOT / "runtime/postgresql/postgresql-20260829T084826Z/root"
    return {
        "root": package_root,
        "bin": package_root / "usr/lib/postgresql/14/bin",
        "lib": package_root / "usr/lib/x86_64-linux-gnu",
        "share": package_root / "usr/share/postgresql/14",
        "data": RUNTIME / "postgresql/data",
        # PostgreSQL limits Unix-domain socket paths to 107 bytes. The fully
        # qualified project path exceeds that limit, so use a Task-2-specific
        # short socket directory while keeping data/logs in the runtime tree.
        "socket": Path("/tmp/pg-task2-55439"),
        "logs": RUNTIME / "postgresql/logs",
    }


def pg_env(paths: dict[str, Path]) -> dict[str, str]:
    env = os.environ.copy()
    current = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = str(paths["lib"]) + ((":" + current) if current else "")
    return env


def setup_postgres() -> tuple[subprocess.Popen, dict[str, int]]:
    paths = pg_paths()
    for key in ("data", "socket", "logs"):
        if paths[key].exists():
            raise RuntimeError(f"refusing to overwrite PostgreSQL {key}: {paths[key]}")
        paths[key].mkdir(parents=True)
    env = pg_env(paths)
    subprocess.run([
        str(paths["bin"] / "initdb"), "-D", str(paths["data"]), "-L", str(paths["share"]),
        "--encoding=UTF8", "--locale=C.UTF-8", "--auth-local=trust", "--auth-host=reject", f"--username={PG_USER}",
    ], check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    config = paths["data"] / "postgresql.conf"
    with config.open("a", encoding="utf-8") as handle:
        handle.write("\nlisten_addresses = ''\n")
        handle.write(f"port = {PG_PORT}\n")
        handle.write(f"unix_socket_directories = '{paths['socket']}'\n")
        handle.write("unix_socket_permissions = 0700\n")
        handle.write("jit = off\nmax_parallel_workers_per_gather = 0\nshared_buffers = '128MB'\nwork_mem = '16MB'\n")
    process = subprocess.Popen([
        str(paths["bin"] / "postgres"), "-D", str(paths["data"]),
    ], env=env, stdout=(paths["logs"] / "postgres.log").open("x"), stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("Task 2 PostgreSQL exited during startup")
        try:
            conn = psycopg2.connect(host=str(paths["socket"]), port=PG_PORT, dbname="postgres", user=PG_USER, connect_timeout=1)
            conn.autocommit = True
            with conn.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE {PG_DATABASE}")
            conn.close()
            break
        except psycopg2.OperationalError:
            time.sleep(0.1)
    else:
        raise RuntimeError("Task 2 PostgreSQL did not become ready")

    conn = pg_connect()
    with conn.cursor() as cursor:
        # The released PostgreSQL schema already creates plpython3u.
        cursor.execute((UPSTREAM / "engines/postgres/scripts/postgres_schema.sql").read_text(encoding="utf-8"))
    conn.commit()
    tables = [
        "artifacts", "artifact_abstracts", "artifact_authorlists", "artifact_authors",
        "artifact_charges", "artifact_citations", "projects", "projects_artifacts",
        "project_artifactcount", "views_stats",
    ]
    counts: dict[str, int] = {}
    csv_root = UPSTREAM / "dataset/csvs/tiny"
    with conn.cursor() as cursor:
        for table in tables:
            with (csv_root / f"{table}.csv").open("r", encoding="utf-8", newline="") as handle:
                cursor.copy_expert(f"COPY {table} FROM STDIN WITH (FORMAT CSV, QUOTE '\"')", handle)
            cursor.execute(f"SELECT count(*) FROM {table}")
            counts[table] = cursor.fetchone()[0]
        cursor.execute("ANALYZE")
    conn.commit()
    install_postgres_udfs(conn)
    conn.close()
    return process, counts


def pg_connect():
    paths = pg_paths()
    connection = psycopg2.connect(host=str(paths["socket"]), port=PG_PORT, dbname=PG_DATABASE, user=PG_USER, connect_timeout=5)
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = '60s'")
        cursor.execute("SET lock_timeout = '5s'")
        cursor.execute("SET jit = off")
        cursor.execute("SET max_parallel_workers_per_gather = 0")
    return connection


def install_postgres_udfs(connection) -> None:
    needed = {
        "scalar": ["extractyear", "extractmonth", "extractday", "jsoncount", "clean", "converttoeuro", "jsonparse", "cleandate", "lowerize", "keywords", "filterstopwords", "stem", "log_10"],
        "aggregate": ["aggregate_avg", "aggregate_median", "aggregate_count", "aggregate_max"],
        "table": ["file", "combinations", "kmeans_iterative", "strsplitv", "jgroupordered"],
    }
    with connection.cursor() as cursor:
        for family, names in needed.items():
            for name in names:
                source = UPSTREAM / "engines/postgres/udfs" / family / f"{name}.sql"
                cursor.execute(source.read_text(encoding="utf-8"))
    connection.commit()


def stop_postgres(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    paths = pg_paths()
    env = pg_env(paths)
    subprocess.run([str(paths["bin"] / "pg_ctl"), "-D", str(paths["data"]), "-m", "fast", "stop"], env=env, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.terminate()


def capture_duckdb(db_path: Path, outcomes: dict[str, Any], events) -> None:
    conn = duckdb.connect(str(db_path))
    conn.execute("PRAGMA threads=1")
    register_duckdb(conn)
    functions = conn.execute("SELECT function_name, function_type, parameters, return_type, has_side_effects FROM duckdb_functions() WHERE function_name IN ('extractyear','extractmonth','extractday','jsoncount','aggregate_avg','aggregate_count','kmeans_iterative','jsonparse_q14','cleandate','lowerize','keywords','filterstopwords','stem','strsplitv','jgroupordered','log_10') ORDER BY function_name").fetchall()
    write_json_new(PLANS / "duckdb/function_metadata.json", {"source": "duckdb_functions()", "rows": functions})
    for workload in SELECTED:
        sql = read_sql("duckdb", workload)
        try:
            plan_rows = conn.execute("EXPLAIN " + sql).fetchall()
            write_json_new(PLANS / f"duckdb/{workload.lower()}_natural_plan.json", {"classification_input": "natural released SQL", "sql": sql, "plan": plan_rows})
            random.seed(SEED)
            np.random.seed(SEED)
            with deadline(TIMEOUT_SECONDS):
                rows = conn.execute(sql).fetchall()
            outcomes.setdefault(workload, {})["DuckDB"] = {"status": "PASS", **canonical_rows(rows, workload)}
        except Exception as exc:
            outcomes.setdefault(workload, {})["DuckDB"] = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
            log_event(events, system="DuckDB", workload=workload, phase="PLAN_OR_SEMANTIC_VALIDATION", status="FAIL", error=f"{type(exc).__name__}: {exc}")
    conn.close()


def capture_sqlite(db_path: Path, outcomes: dict[str, Any], events) -> None:
    import sqlite3

    conn = sqlite3.connect(db_path)
    register_sqlite(conn)
    write_json_new(PLANS / "sqlite/function_metadata.json", {"source": "application registration; SQLite catalog has no scalar-UDF cost/structure metadata", "registered_from": "canonical UDFBench engines/sqlite/udfs source", "optimizer_visible_procedural_structure": False})
    for workload in SELECTED:
        if workload not in SQLITE_SUPPORTED:
            outcomes.setdefault(workload, {})["SQLite"] = {"status": "UNSUPPORTED_BY_SYSTEM", "reason": "Selected workload is absent from the released direct SQLite adapter; the separate APSW virtual-table path was not introduced."}
            continue
        sql = read_sql("sqlite", workload)
        try:
            plan_rows = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
            write_json_new(PLANS / f"sqlite/{workload.lower()}_natural_plan.json", {"classification_input": "natural released SQL", "sql": sql, "plan": plan_rows})
            with deadline(TIMEOUT_SECONDS):
                rows = conn.execute(sql).fetchall()
            outcomes.setdefault(workload, {})["SQLite"] = {"status": "PASS", **canonical_rows(rows, workload)}
        except Exception as exc:
            outcomes.setdefault(workload, {})["SQLite"] = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
            log_event(events, system="SQLite", workload=workload, phase="PLAN_OR_SEMANTIC_VALIDATION", status="FAIL", error=f"{type(exc).__name__}: {exc}")
    conn.close()


def capture_postgres(outcomes: dict[str, Any], events) -> None:
    conn = pg_connect()
    function_names = ["extractyear", "extractmonth", "extractday", "jsoncount", "clean", "converttoeuro", "jsonparse", "cleandate", "lowerize", "keywords", "filterstopwords", "stem", "log_10", "aggregate_avg", "aggregate_count", "aggregate_max", "file", "combinations", "kmeans_iterative", "strsplitv", "jgroupordered"]
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT p.proname, l.lanname, p.provolatile, p.proparallel, p.proisstrict,
                   p.procost::float8, p.prorows::float8, pg_get_function_identity_arguments(p.oid)
            FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang
            WHERE p.proname = ANY(%s) ORDER BY p.proname, pg_get_function_identity_arguments(p.oid)
        """, (function_names,))
        metadata = cursor.fetchall()
    write_json_new(PLANS / "postgresql/function_metadata.json", {"source": "pg_proc", "columns": ["name", "language", "volatility_code", "parallel_code", "strict", "cost", "rows", "arguments"], "rows": metadata})
    for workload in SELECTED:
        sql = read_sql("postgres", workload)
        try:
            with conn.cursor() as cursor:
                cursor.execute("EXPLAIN (VERBOSE TRUE, COSTS TRUE, FORMAT JSON) " + sql)
                plan = cursor.fetchone()[0]
            write_json_new(PLANS / f"postgresql/{workload.lower()}_natural_plan.json", {"classification_input": "natural released SQL without EXPLAIN ANALYZE wrapper", "sql": sql, "plan": plan})
            random.seed(SEED)
            np.random.seed(SEED)
            with conn.cursor() as cursor:
                cursor.execute(sql)
                rows = cursor.fetchall()
            outcomes.setdefault(workload, {})["PostgreSQL"] = {"status": "PASS", **canonical_rows(rows, workload)}
        except Exception as exc:
            outcomes.setdefault(workload, {})["PostgreSQL"] = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
            log_event(events, system="PostgreSQL", workload=workload, phase="PLAN_OR_SEMANTIC_VALIDATION", status="FAIL", error=f"{type(exc).__name__}: {exc}")
    conn.close()


def semantic_comparability(outcomes: dict[str, Any]) -> dict[str, Any]:
    comparisons: dict[str, Any] = {}
    for workload, per_system in outcomes.items():
        passed = {system: result for system, result in per_system.items() if result.get("status") == "PASS"}
        invariant_groups: dict[str, list[str]] = {}
        for system, result in passed.items():
            invariant_groups.setdefault(result["semantic_invariant_sha256"], []).append(system)
        comparisons[workload] = {
            "passing_systems": sorted(passed),
            "semantic_invariant_groups": invariant_groups,
            "cross_system_equivalent": len(passed) >= 2 and len(invariant_groups) == 1,
            "primary_timing_systems": sorted(passed),
            "note": "Cross-system output mismatch does not invalidate within-system timing, but it excludes a comparable cross-system semantic claim.",
        }
    return comparisons


def run_trial_sqlite(db_path: Path, workload: str) -> tuple[list[tuple[Any, ...]], float]:
    import sqlite3
    conn = sqlite3.connect(db_path)
    register_sqlite(conn)
    sql = read_sql("sqlite", workload)
    start = time.perf_counter_ns()
    rows = conn.execute(sql).fetchall()
    elapsed = (time.perf_counter_ns() - start) / 1_000_000
    conn.close()
    return rows, elapsed


def run_trial_duckdb(db_path: Path, workload: str) -> tuple[list[tuple[Any, ...]], float]:
    conn = duckdb.connect(str(db_path))
    conn.execute("PRAGMA threads=1")
    register_duckdb(conn)
    random.seed(SEED)
    np.random.seed(SEED)
    sql = read_sql("duckdb", workload)
    start = time.perf_counter_ns()
    rows = conn.execute(sql).fetchall()
    elapsed = (time.perf_counter_ns() - start) / 1_000_000
    conn.close()
    return rows, elapsed


def run_trial_postgres(workload: str) -> tuple[list[tuple[Any, ...]], float]:
    conn = pg_connect()
    sql = read_sql("postgres", workload)
    start = time.perf_counter_ns()
    with conn.cursor() as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall()
    elapsed = (time.perf_counter_ns() - start) / 1_000_000
    conn.close()
    return rows, elapsed


def run_trials(system: str, supported: set[str], outcomes: dict[str, Any], raw_writer, raw_handle, events, db_path: Path | None = None) -> None:
    runnable = [workload for workload in SELECTED if workload in supported and outcomes.get(workload, {}).get(system, {}).get("status") == "PASS"]
    runner: Callable[[str], tuple[list[tuple[Any, ...]], float]]
    if system == "SQLite":
        assert db_path is not None
        runner = lambda workload: run_trial_sqlite(db_path, workload)
    elif system == "DuckDB":
        assert db_path is not None
        runner = lambda workload: run_trial_duckdb(db_path, workload)
    else:
        runner = run_trial_postgres
    blocks = [("warmup", index) for index in range(1, WARMUPS + 1)] + [("measured", index) for index in range(1, REPETITIONS + 1)]
    for phase, block in blocks:
        order = list(runnable)
        random.Random(SEED + block + (0 if phase == "warmup" else 1000) + {"SQLite": 10000, "DuckDB": 20000, "PostgreSQL": 30000}[system]).shuffle(order)
        for position, workload in enumerate(order, start=1):
            status = "PASS"
            error = ""
            elapsed_ms = ""
            row_count = ""
            output_sha = ""
            invariant_sha = ""
            try:
                with deadline(TIMEOUT_SECONDS):
                    rows, elapsed = runner(workload)
                canonical = canonical_rows(rows, workload)
                elapsed_ms = f"{elapsed:.6f}"
                row_count = str(canonical["row_count"])
                output_sha = canonical["output_sha256"]
                invariant_sha = canonical["semantic_invariant_sha256"]
            except Exception as exc:
                status = "TIMEOUT" if isinstance(exc, Timeout) or "statement timeout" in str(exc).lower() else "FAIL"
                error = f"{type(exc).__name__}: {exc}"
                log_event(events, system=system, workload=workload, phase=f"PRIMARY_{phase.upper()}", block=block, status=status, error=error)
            raw_writer.writerow({
                "run_id": "task2-udfbench-20260902", "system": system, "workload": workload,
                "phase": phase, "block": block, "position": position, "seed": SEED,
                "timeout_seconds": TIMEOUT_SECONDS, "wall_time_ms": elapsed_ms,
                "status": status, "row_count": row_count, "output_sha256": output_sha,
                "semantic_invariant_sha256": invariant_sha, "error": error,
            })
            raw_handle.flush()


def diagnostics() -> dict[str, Any]:
    # Oracle/source diagnostics are separate from primary timing and unavailable to planners.
    import sqlite3
    db = RUNTIME / "sqlite/udfbench_tiny.sqlite"
    conn = sqlite3.connect(db)
    register_sqlite(conn)
    result: dict[str, Any] = {"classification": "DIAGNOSTIC_ONLY", "planning_time_information": False}
    result["Q1"] = {
        "artifact_rows": conn.execute("SELECT count(*) FROM artifacts").fetchone()[0],
        "non_null_dates": conn.execute("SELECT count(*) FROM artifacts WHERE date IS NOT NULL").fetchone()[0],
        "potential_scalar_invocations": conn.execute("SELECT 3*count(*) FROM artifacts WHERE date IS NOT NULL").fetchone()[0],
    }
    result["Q8"] = {
        "join_rows": conn.execute("SELECT count(*) FROM artifact_citations c JOIN artifact_authorlists a ON c.artifactid=a.artifactid").fetchone()[0],
        "sum_target_json_counts": conn.execute("SELECT sum(jsoncount(target)) FROM artifact_citations c JOIN artifact_authorlists a ON c.artifactid=a.artifactid").fetchone()[0],
        "sum_authorlist_json_counts": conn.execute("SELECT sum(jsoncount(authorlist)) FROM artifact_citations c JOIN artifact_authorlists a ON c.artifactid=a.artifactid").fetchone()[0],
    }
    conn.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default="task2-udfbench-20260902")
    args = parser.parse_args()
    prereg = json.loads(PREREG.read_text(encoding="utf-8"))
    if prereg["status"] != "FROZEN_BEFORE_PRIMARY_EXECUTION" or tuple(item["workload_id"] for item in prereg["selected_workloads"]) != SELECTED:
        raise SystemExit("preregistration does not match runner")
    if subprocess.check_output(["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"], text=True).strip() != COMMIT:
        raise SystemExit("UDFBench commit mismatch")

    RAW.mkdir(parents=True, exist_ok=True)
    PLANS.mkdir(parents=True, exist_ok=True)
    # Attempts 1--4 stopped during setup/metadata capture before timings.
    # Preserve them verbatim and use non-overwriting attempt-5 files.
    events_path = RAW / "task2-udfbench-20260902_attempt5_execution_events.jsonl"
    trials_path = RAW / "task2-udfbench-20260902_attempt5_trials.csv"
    sqlite_db = RUNTIME / "sqlite/udfbench_tiny.sqlite"
    duckdb_db = RUNTIME / "duckdb/udfbench_tiny.duckdb"
    sqlite_db.parent.mkdir(parents=True, exist_ok=False)
    duckdb_db.parent.mkdir(parents=True, exist_ok=False)
    pg_process: subprocess.Popen | None = None
    outcomes: dict[str, Any] = {}
    load_counts: dict[str, dict[str, int]] = {}

    fields = ["run_id", "system", "workload", "phase", "block", "position", "seed", "timeout_seconds", "wall_time_ms", "status", "row_count", "output_sha256", "semantic_invariant_sha256", "error"]
    with events_path.open("x", encoding="utf-8") as events, trials_path.open("x", newline="", encoding="utf-8") as raw_handle:
        raw_writer = csv.DictWriter(raw_handle, fieldnames=fields)
        raw_writer.writeheader()
        try:
            load_counts["SQLite"] = setup_sqlite(sqlite_db)
            log_event(events, system="SQLite", phase="SETUP", status="PASS", row_counts=load_counts["SQLite"])
            load_counts["DuckDB"] = setup_duckdb(duckdb_db)
            log_event(events, system="DuckDB", phase="SETUP", status="PASS", row_counts=load_counts["DuckDB"])
            pg_process, load_counts["PostgreSQL"] = setup_postgres()
            log_event(events, system="PostgreSQL", phase="SETUP", status="PASS", row_counts=load_counts["PostgreSQL"])

            capture_sqlite(sqlite_db, outcomes, events)
            capture_duckdb(duckdb_db, outcomes, events)
            capture_postgres(outcomes, events)
            comparisons = semantic_comparability(outcomes)
            write_json_new(METADATA / "semantic_validation.json", {"outcomes": outcomes, "comparisons": comparisons})
            write_json_new(METADATA / "loaded_row_counts.json", load_counts)
            write_json_new(METADATA / "procedural_work_diagnostics.json", diagnostics())

            run_trials("SQLite", SQLITE_SUPPORTED, outcomes, raw_writer, raw_handle, events, sqlite_db)
            run_trials("DuckDB", DUCKDB_SUPPORTED, outcomes, raw_writer, raw_handle, events, duckdb_db)
            run_trials("PostgreSQL", POSTGRES_SUPPORTED, outcomes, raw_writer, raw_handle, events)
        finally:
            stop_postgres(pg_process)

    write_json_new(METADATA / "execution_complete.json", {
        "run_id": args.run_id,
        "status": "COMPLETE",
        "udfbench_commit": COMMIT,
        "raw_trials": str(trials_path.relative_to(ROOT)),
        "plans": str(PLANS.relative_to(ROOT)),
        "postgres_stopped": True,
    })
    print(stable_json({"status": "COMPLETE", "raw_trials": str(trials_path), "semantic_validation": str(METADATA / "semantic_validation.json")}))


if __name__ == "__main__":
    main()
