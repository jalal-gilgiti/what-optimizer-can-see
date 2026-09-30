#!/usr/bin/env python3
"""Run the preregistered Task 11 UDFBench official-small validation."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import random
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import duckdb
import numpy as np
import psycopg2


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "runtime/task2_udfbench_validation/UDFBench"
RUNTIME = ROOT / "runtime/task11_udfbench_scale_validation"
RAW = ROOT / "raw/task11_udfbench_scale_validation"
PLANS = ROOT / "plans/task11_udfbench_scale_validation"
META = ROOT / "metadata/task11_udfbench_scale_validation"
DATASET_MANIFEST = META / "dataset_manifest.json"
PREREG = META / "preregistration.md"
TASK2_RUNNER = ROOT / "scripts/run/run_task2_udfbench.py"
COMMIT = "8da987566590e2534f24cd97bcfaa9157c24962c"
RUN_ID = "task11-udfbench-small-20260906"
ATTEMPT = 3
SELECTED = ("Q8", "Q14", "Q17")
CASES = {
    "SQLite": ("Q8", "Q14"),
    "DuckDB": SELECTED,
    "PostgreSQL": ("Q8", "Q14"),
}
SEED = 20260906
PG_PORT = 55451
PG_USER = "<REDACTED_USER>"
PG_DATABASE = "udfbench_task11_small"
TABLES = (
    "artifacts", "artifact_abstracts", "artifact_authorlists", "artifact_authors",
    "artifact_charges", "artifact_citations", "projects", "projects_artifacts",
    "project_artifactcount", "views_stats",
)
STARTED_PG_PROCESS: subprocess.Popen | None = None


def load_task2_module():
    spec = importlib.util.spec_from_file_location("frozen_task2_runner", TASK2_RUNNER)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load frozen Task 2 runner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.sha256(TASK2_RUNNER) != "e3f0146982c3dbf42e05a64e85a23d73aa6a1557da03ac670583bb7ceb070135":
        # This hard gate is updated only if the before-manifest proves the file was
        # frozen under another hash. It prevents silent helper drift during Task 11.
        raise RuntimeError("frozen Task 2 runner hash does not match Task 11 adapter")
    return module


T2 = load_task2_module()


def write_json_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")


def log(handle, **value: Any) -> None:
    handle.write(json.dumps({"timestamp_ns": time.time_ns(), **value}, sort_keys=True, default=str) + "\n")
    handle.flush()


class TrialTimeout(Exception):
    pass


@contextmanager
def deadline(seconds: int):
    def handler(_signum, _frame):
        raise TrialTimeout(f"exceeded {seconds}s")

    old = signal.signal(signal.SIGALRM, handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def data_root() -> Path:
    manifest = json.loads(DATASET_MANIFEST.read_text(encoding="utf-8"))
    roots = manifest["csv_roots"]
    if len(roots) != 1:
        raise RuntimeError(f"expected one official small CSV root, got {roots}")
    result = ROOT / roots[0]
    if not result.is_dir():
        raise RuntimeError(f"official small CSV root missing: {result}")
    return result


def read_sql(system: str, workload: str) -> str:
    engine = {"SQLite": "sqlite", "DuckDB": "duckdb", "PostgreSQL": "postgres"}[system]
    return T2.read_sql(engine, workload)


def row_fingerprint(rows: Iterator[tuple[Any, ...]]) -> dict[str, Any]:
    modulus = 1 << 256
    total = 0
    xor = 0
    count = 0
    null_cells = 0
    samples: list[Any] = []
    for row in rows:
        normalized = [T2.normalize_value(value) for value in row]
        encoded = T2.stable_json(normalized).encode("utf-8")
        number = int.from_bytes(hashlib.sha256(encoded).digest(), "big")
        total = (total + number) % modulus
        xor ^= number
        count += 1
        null_cells += sum(value is None for value in row)
        if len(samples) < 10:
            samples.append(normalized)
    multiset = {"row_count": count, "sum256": f"{total:064x}", "xor256": f"{xor:064x}"}
    return {
        **multiset,
        "multiset_sha256": hashlib.sha256(T2.stable_json(multiset).encode("utf-8")).hexdigest(),
        "null_cell_count": null_cells,
        "sample_in_return_order": samples,
        "hash_method": "order-independent SHA-256 row digest sum+xor; normalized values",
    }


def batches(cursor, size: int = 10_000) -> Iterator[tuple[Any, ...]]:
    while True:
        block = cursor.fetchmany(size)
        if not block:
            return
        yield from block


def setup_sqlite(path: Path, csv_root: Path) -> dict[str, int]:
    import sqlite3

    if path.exists():
        raise RuntimeError(f"refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=False)
    connection = sqlite3.connect(path)
    csv.field_size_limit(min(sys.maxsize, 2_147_483_647))
    connection.executescript((SOURCE / "engines/sqlite/scripts/sqlite_schema.sql").read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for table in TABLES:
        width = len(connection.execute(f"PRAGMA table_info({table})").fetchall())
        placeholders = ",".join("?" for _ in range(width))
        with (csv_root / f"{table}.csv").open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            while True:
                block = []
                for _ in range(10_000):
                    try:
                        row = next(reader)
                    except StopIteration:
                        break
                    if len(row) != width:
                        raise RuntimeError(f"{table}: CSV width {len(row)} != schema width {width}")
                    block.append(tuple(None if value == "" else value for value in row))
                if not block:
                    break
                connection.executemany(f"INSERT INTO {table} VALUES ({placeholders})", block)
        counts[table] = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        connection.commit()
    connection.execute("ANALYZE")
    connection.commit()
    connection.close()
    return counts


def setup_duckdb(path: Path, csv_root: Path) -> dict[str, int]:
    if path.exists():
        raise RuntimeError(f"refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=False)
    connection = duckdb.connect(str(path))
    connection.execute("PRAGMA threads=1")
    connection.execute((SOURCE / "engines/duckdb/scripts/duckdb_schema.sql").read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for table in TABLES:
        source = str((csv_root / f"{table}.csv").resolve()).replace("'", "''")
        connection.execute(f"COPY {table} FROM '{source}' (FORMAT CSV, QUOTE '\"', DELIMITER ',', HEADER FALSE)")
        counts[table] = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    connection.execute("CHECKPOINT")
    connection.close()
    return counts


def pg_paths() -> dict[str, Path]:
    package_root = ROOT / "runtime/postgresql/postgresql-20260829T084826Z/root"
    return {
        "bin": package_root / "usr/lib/postgresql/14/bin",
        "lib": package_root / "usr/lib/x86_64-linux-gnu",
        "share": package_root / "usr/share/postgresql/14",
        "data": RUNTIME / "postgresql/data",
        "socket": Path("/tmp/pg-task11-55451"),
        "logs": RUNTIME / "postgresql/logs",
    }


def pg_env() -> dict[str, str]:
    paths = pg_paths()
    env = os.environ.copy()
    current = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = str(paths["lib"]) + ((":" + current) if current else "")
    return env


def pg_connect():
    paths = pg_paths()
    connection = psycopg2.connect(host=str(paths["socket"]), port=PG_PORT, dbname=PG_DATABASE, user=PG_USER, connect_timeout=5)
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = '900s'")
        cursor.execute("SET lock_timeout = '5s'")
        cursor.execute("SET jit = off")
        cursor.execute("SET max_parallel_workers_per_gather = 0")
    return connection


def setup_postgres(csv_root: Path) -> tuple[subprocess.Popen, dict[str, int]]:
    global STARTED_PG_PROCESS
    paths = pg_paths()
    for name in ("data", "socket", "logs"):
        if paths[name].exists():
            raise RuntimeError(f"refusing to overwrite Task 11 PostgreSQL {name}: {paths[name]}")
        paths[name].mkdir(parents=True)
    env = pg_env()
    subprocess.run([
        str(paths["bin"] / "initdb"), "-D", str(paths["data"]), "-L", str(paths["share"]),
        "--encoding=UTF8", "--locale=C.UTF-8", "--auth-local=trust", "--auth-host=reject", f"--username={PG_USER}",
    ], check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    with (paths["data"] / "postgresql.conf").open("a", encoding="utf-8") as handle:
        handle.write("\nlisten_addresses = ''\n")
        handle.write(f"port = {PG_PORT}\n")
        handle.write(f"unix_socket_directories = '{paths['socket']}'\n")
        handle.write("unix_socket_permissions = 0700\n")
        handle.write("jit = off\nmax_parallel_workers_per_gather = 0\nshared_buffers = '256MB'\nwork_mem = '32MB'\n")
    log_handle = (paths["logs"] / "postgres.log").open("x", encoding="utf-8")
    process = subprocess.Popen([str(paths["bin"] / "postgres"), "-D", str(paths["data"])], env=env, stdout=log_handle, stderr=subprocess.STDOUT, text=True)
    STARTED_PG_PROCESS = process
    for _ in range(200):
        if process.poll() is not None:
            raise RuntimeError("isolated Task 11 PostgreSQL exited during startup")
        try:
            connection = psycopg2.connect(host=str(paths["socket"]), port=PG_PORT, dbname="postgres", user=PG_USER, connect_timeout=1)
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE {PG_DATABASE}")
            connection.close()
            break
        except psycopg2.OperationalError:
            time.sleep(0.1)
    else:
        raise RuntimeError("isolated Task 11 PostgreSQL did not become ready")
    connection = pg_connect()
    with connection.cursor() as cursor:
        cursor.execute((SOURCE / "engines/postgres/scripts/postgres_schema.sql").read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    with connection.cursor() as cursor:
        for table in TABLES:
            with (csv_root / f"{table}.csv").open("r", encoding="utf-8", newline="") as handle:
                cursor.copy_expert(f"COPY {table} FROM STDIN WITH (FORMAT CSV, QUOTE '\"')", handle)
            cursor.execute(f"SELECT count(*) FROM {table}")
            counts[table] = cursor.fetchone()[0]
        cursor.execute("ANALYZE")
    T2.install_postgres_udfs(connection)
    connection.close()
    return process, counts


def stop_postgres(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    paths = pg_paths()
    subprocess.run([str(paths["bin"] / "pg_ctl"), "-D", str(paths["data"]), "-m", "fast", "stop"], env=pg_env(), check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.terminate()


def open_cursor(system: str, database: Path | None):
    if system == "SQLite":
        import sqlite3
        connection = sqlite3.connect(database)
        T2.register_sqlite(connection)
        return connection, connection.cursor()
    if system == "DuckDB":
        connection = duckdb.connect(str(database))
        connection.execute("PRAGMA threads=1")
        T2.register_duckdb(connection)
        return connection, connection.cursor()
    connection = pg_connect()
    return connection, connection.cursor()


def capture_plans_and_metadata(databases: dict[str, Path | None]) -> None:
    for system, workloads in CASES.items():
        connection, cursor = open_cursor(system, databases[system])
        try:
            if system == "SQLite":
                write_json_new(PLANS / "sqlite/function_metadata.json", {
                    "source": "application registration from pinned UDFBench source",
                    "optimizer_visible_procedural_structure": False,
                })
            elif system == "DuckDB":
                cursor.execute("SELECT function_name, function_type, parameters, return_type, has_side_effects FROM duckdb_functions() WHERE function_name IN ('jsoncount','aggregate_avg','jsonparse_q14','cleandate','lowerize','keywords','filterstopwords','stem','strsplitv','jgroupordered','log_10') ORDER BY function_name")
                write_json_new(PLANS / "duckdb/function_metadata.json", {"source": "duckdb_functions()", "rows": cursor.fetchall()})
            else:
                cursor.execute("""
                    SELECT p.proname,l.lanname,p.provolatile,p.proparallel,p.proisstrict,
                           p.procost::float8,p.prorows::float8,pg_get_function_identity_arguments(p.oid)
                    FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang
                    WHERE p.proname=ANY(%s) ORDER BY p.proname,pg_get_function_identity_arguments(p.oid)
                """, (["jsoncount", "aggregate_avg", "jsonparse", "cleandate", "aggregate_max"],))
                write_json_new(PLANS / "postgresql/function_metadata.json", {"source": "pg_proc", "rows": cursor.fetchall()})
            for workload in workloads:
                sql = read_sql(system, workload)
                started = time.perf_counter_ns()
                folder = system.lower().replace("postgresql", "postgresql")
                path = PLANS / folder / f"{workload.lower()}_small_natural_plan.json"
                try:
                    with deadline(900 if workload == "Q17" else 600):
                        if system == "SQLite":
                            cursor.execute("EXPLAIN QUERY PLAN " + sql)
                        elif system == "DuckDB":
                            cursor.execute("EXPLAIN " + sql)
                        else:
                            cursor.execute("EXPLAIN (VERBOSE TRUE, COSTS TRUE, FORMAT JSON) " + sql)
                        plan = cursor.fetchall()
                    wall_ms = (time.perf_counter_ns() - started) / 1_000_000
                    write_json_new(path, {
                        "run_id": RUN_ID, "system": system, "scale": "small", "workload": workload,
                        "status": "CAPTURED",
                        "classification_input": "unmodified released natural SQL; released PostgreSQL EXPLAIN wrapper removed",
                        "sql": sql, "plan_capture_wall_ms": wall_ms, "plan": plan,
                    })
                except Exception as exc:
                    write_json_new(path, {
                        "run_id": RUN_ID, "system": system, "scale": "small", "workload": workload,
                        "status": "TIMEOUT" if isinstance(exc, TrialTimeout) or "timeout" in str(exc).lower() else "FAIL",
                        "classification_input": "unmodified released natural SQL; released PostgreSQL EXPLAIN wrapper removed",
                        "sql": sql, "plan_capture_wall_ms": (time.perf_counter_ns() - started) / 1_000_000,
                        "error": f"{type(exc).__name__}: {exc}", "plan": [],
                    })
        finally:
            cursor.close()
            connection.close()


def semantic_case(system: str, workload: str, database: Path | None, timeout: int) -> dict[str, Any]:
    connection, cursor = open_cursor(system, database)
    sql = read_sql(system, workload)
    try:
        random.seed(SEED)
        np.random.seed(SEED)
        with deadline(timeout):
            cursor.execute(sql)
            result = row_fingerprint(batches(cursor))
        return {"status": "PASS", **result}
    except Exception as exc:
        return {"status": "TIMEOUT" if isinstance(exc, TrialTimeout) or "timeout" in str(exc).lower() else "FAIL", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        cursor.close()
        connection.close()


def validate_semantics(databases: dict[str, Path | None], events) -> dict[str, Any]:
    outcomes: dict[str, Any] = {workload: {} for workload in SELECTED}
    for system, workloads in CASES.items():
        for workload in workloads:
            timeout = 900 if workload == "Q17" else 600
            result = semantic_case(system, workload, databases[system], timeout)
            if result.get("status") == "PASS" and (
                (workload == "Q8" and result.get("row_count") != 1)
                or (workload in {"Q14", "Q17"} and not result.get("row_count"))
            ):
                result["status"] = "INVALID_OUTPUT"
                result["error"] = "released query returned an implausible empty/wrong-cardinality result"
            outcomes[workload][system] = result
            log(events, phase="SEMANTIC_VALIDATION", system=system, workload=workload, status=result["status"], error=result.get("error", ""))
    outcomes["Q17"]["SQLite"] = {"status": "ENGINE_UNSUPPORTED", "reason": "no released direct SQLite adapter for Q17"}
    outcomes["Q17"]["PostgreSQL"] = {"status": "DIALECT_FAILURE_FROZEN_TASK2", "reason": "released PostgreSQL Q17 fails on unnest(record); not repaired"}
    comparisons: dict[str, Any] = {}
    for workload in SELECTED:
        passed = {system: result for system, result in outcomes[workload].items() if result.get("status") == "PASS"}
        groups: dict[str, list[str]] = {}
        for system, result in passed.items():
            groups.setdefault(result["multiset_sha256"], []).append(system)
        comparisons[workload] = {
            "passing_systems": sorted(passed),
            "hash_groups": groups,
            "cross_system_equivalent": len(passed) >= 2 and len(groups) == 1,
            "timing_gate_pass": (len(passed) >= 2 and len(groups) == 1) if workload in {"Q8", "Q14"} else False,
        }
    if outcomes["Q17"]["DuckDB"].get("status") == "PASS":
        repeat = semantic_case("DuckDB", "Q17", databases["DuckDB"], 900)
        outcomes["Q17"]["DuckDB_repeat"] = repeat
        comparisons["Q17"]["repeat_stable"] = repeat.get("multiset_sha256") == outcomes["Q17"]["DuckDB"].get("multiset_sha256")
        comparisons["Q17"]["timing_gate_pass"] = comparisons["Q17"]["repeat_stable"]
        log(events, phase="SEMANTIC_REPEAT", system="DuckDB", workload="Q17", status=repeat["status"], error=repeat.get("error", ""))
    return {"run_id": RUN_ID, "scale": "small", "outcomes": outcomes, "comparisons": comparisons}


def timed_case(system: str, workload: str, database: Path | None, timeout: int) -> tuple[float, int]:
    connection, cursor = open_cursor(system, database)
    sql = read_sql(system, workload)
    try:
        random.seed(SEED)
        np.random.seed(SEED)
        with deadline(timeout):
            started = time.perf_counter_ns()
            cursor.execute(sql)
            count = 0
            while True:
                block = cursor.fetchmany(10_000)
                if not block:
                    break
                count += len(block)
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        return elapsed_ms, count
    finally:
        cursor.close()
        connection.close()


def run_trials(databases: dict[str, Path | None], semantic: dict[str, Any], writer, raw_handle, events) -> None:
    for system, workloads in CASES.items():
        runnable = [workload for workload in workloads if semantic["comparisons"][workload]["timing_gate_pass"] and semantic["outcomes"][workload][system]["status"] == "PASS"]
        maximum = max((13 if workload != "Q17" else 6) for workload in runnable) if runnable else 0
        for block_index in range(1, maximum + 1):
            block_workloads = [workload for workload in runnable if block_index <= (13 if workload != "Q17" else 6)]
            random.Random(SEED + block_index + {"SQLite": 10_000, "DuckDB": 20_000, "PostgreSQL": 30_000}[system]).shuffle(block_workloads)
            for position, workload in enumerate(block_workloads, 1):
                warmups = 1 if workload == "Q17" else 3
                phase = "warmup" if block_index <= warmups else "measured"
                trial = block_index if phase == "warmup" else block_index - warmups
                timeout = 900 if workload == "Q17" else 600
                status, elapsed, rows, error = "PASS", "", "", ""
                try:
                    value, count = timed_case(system, workload, databases[system], timeout)
                    elapsed, rows = f"{value:.6f}", str(count)
                    expected = semantic["outcomes"][workload][system]["row_count"]
                    if count != expected:
                        status, error = "FAIL", f"row count {count} != semantic gate {expected}"
                except Exception as exc:
                    status = "TIMEOUT" if isinstance(exc, TrialTimeout) or "timeout" in str(exc).lower() else "FAIL"
                    error = f"{type(exc).__name__}: {exc}"
                writer.writerow({
                    "run_id": RUN_ID, "scale": "small", "system": system, "workload": workload,
                    "phase": phase, "trial": trial, "block": block_index, "position": position,
                    "seed": SEED, "timeout_seconds": timeout, "wall_time_ms": elapsed,
                    "status": status, "row_count": rows, "error": error,
                })
                raw_handle.flush()
                log(events, phase=f"TIMING_{phase.upper()}", system=system, workload=workload, trial=trial, status=status, wall_time_ms=elapsed, error=error)


def diagnostics(duckdb_path: Path, counts: dict[str, int]) -> dict[str, Any]:
    connection = duckdb.connect(str(duckdb_path))
    connection.execute("PRAGMA threads=1")
    T2.register_duckdb(connection)
    result = {
        "classification": "DIAGNOSTIC_ONLY_NOT_OPTIMIZER_VISIBLE",
        "scale": "small",
        "loaded_row_counts": counts,
        "Q8": {
            "join_rows": connection.execute("SELECT count(*) FROM artifact_citations c JOIN artifact_authorlists a ON c.artifactid=a.artifactid").fetchone()[0],
            "sum_target_json_counts": connection.execute("SELECT sum(jsoncount(target)) FROM artifact_citations c JOIN artifact_authorlists a ON c.artifactid=a.artifactid").fetchone()[0],
            "sum_authorlist_json_counts": connection.execute("SELECT sum(jsoncount(authorlist)) FROM artifact_citations c JOIN artifact_authorlists a ON c.artifactid=a.artifactid").fetchone()[0],
        },
        "Q14": {
            "eligible_author_rows": connection.execute("SELECT count(*) FROM artifact_authors WHERE affiliation IS NOT NULL AND affiliation <> '[]' AND authorid IS NOT NULL AND authorid <> '[]'").fetchone()[0],
            "rank1_author_rows": connection.execute("SELECT count(*) FROM artifact_authors WHERE rank=1").fetchone()[0],
            "ec_project_rows": connection.execute("SELECT count(*) FROM projects WHERE funder='European Commission'").fetchone()[0],
            "relationally_qualified_rows_before_latest_date_test": connection.execute("""
                SELECT count(*) FROM artifact_authors aa
                JOIN artifacts a ON aa.artifactid=a.id
                JOIN projects_artifacts pr ON pr.artifactid=a.id
                JOIN projects p ON p.id=pr.projectid
                WHERE aa.affiliation IS NOT NULL AND aa.affiliation <> '[]'
                  AND aa.authorid IS NOT NULL AND aa.authorid <> '[]'
                  AND aa.rank=1 AND p.funder='European Commission'
            """).fetchone()[0],
        },
        "Q17": {
            "abstract_rows": counts["artifact_abstracts"],
            "non_null_abstract_rows": connection.execute("SELECT count(*) FROM artifact_abstracts WHERE abstract IS NOT NULL").fetchone()[0],
        },
    }
    connection.close()
    return result


def environment_manifest() -> dict[str, Any]:
    paths = pg_paths()
    commands = {
        "python": [sys.executable, "--version"],
        "postgres": [str(paths["bin"] / "postgres"), "--version"],
    }
    versions = {}
    for name, command in commands.items():
        completed = subprocess.run(command, env=pg_env(), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True)
        versions[name] = completed.stdout.strip()
    return {
        "created_utc": datetime.now(timezone.utc).isoformat(), "run_id": RUN_ID,
        "versions": {**versions, "sqlite": sqlite3.sqlite_version, "duckdb": duckdb.__version__, "numpy": np.__version__, "psycopg2": psycopg2.__version__},
        "controls": {"duckdb_threads": 1, "postgres_jit": False, "postgres_parallel_gather": 0, "postgres_port": PG_PORT, "postgres_socket": str(paths["socket"]), "postgres_data": str(paths["data"])},
        "python_executable": sys.executable,
    }


def main() -> None:
    overall_started = time.perf_counter()
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=RUN_ID)
    args = parser.parse_args()
    if args.run_id != RUN_ID:
        raise SystemExit(f"Task 11 preregistration fixes RUN_ID={RUN_ID}")
    if "FROZEN_BEFORE_DATA_DOWNLOAD_AND_PRIMARY_TIMING" not in PREREG.read_text(encoding="utf-8"):
        raise SystemExit("Task 11 preregistration is not frozen")
    head = subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(SOURCE), "status", "--porcelain"], text=True)
    if head != COMMIT or dirty:
        raise SystemExit("pinned UDFBench source commit/cleanliness gate failed")
    for path in (RAW, PLANS):
        path.mkdir(parents=True, exist_ok=True)
    environment_path = META / "environment_manifest.json"
    if environment_path.exists():
        existing_environment = json.loads(environment_path.read_text(encoding="utf-8"))
        expected_environment = environment_manifest()
        if existing_environment["run_id"] != RUN_ID or existing_environment["versions"] != expected_environment["versions"] or existing_environment["controls"] != expected_environment["controls"]:
            raise SystemExit("existing Task 11 environment manifest does not match fresh environment")
    else:
        write_json_new(environment_path, environment_manifest())
    events_path = RAW / f"{RUN_ID}_attempt{ATTEMPT}_execution_events.jsonl"
    trials_path = RAW / f"{RUN_ID}_attempt{ATTEMPT}_trials.csv"
    sqlite_path = RUNTIME / "sqlite/udfbench_small.sqlite"
    duckdb_path = RUNTIME / "duckdb/udfbench_small.duckdb"
    postgres_process: subprocess.Popen | None = None
    completed = False
    fields = ["run_id", "scale", "system", "workload", "phase", "trial", "block", "position", "seed", "timeout_seconds", "wall_time_ms", "status", "row_count", "error"]
    with events_path.open("x", encoding="utf-8") as events, trials_path.open("x", encoding="utf-8", newline="") as raw_handle:
        writer = csv.DictWriter(raw_handle, fieldnames=fields)
        writer.writeheader()
        try:
            root = data_root()
            sqlite_counts = setup_sqlite(sqlite_path, root)
            log(events, phase="SETUP", system="SQLite", status="PASS", row_counts=sqlite_counts)
            duckdb_counts = setup_duckdb(duckdb_path, root)
            log(events, phase="SETUP", system="DuckDB", status="PASS", row_counts=duckdb_counts)
            postgres_process, postgres_counts = setup_postgres(root)
            log(events, phase="SETUP", system="PostgreSQL", status="PASS", row_counts=postgres_counts)
            loaded = {"SQLite": sqlite_counts, "DuckDB": duckdb_counts, "PostgreSQL": postgres_counts}
            if len({json.dumps(value, sort_keys=True) for value in loaded.values()}) != 1:
                raise RuntimeError("loaded table cardinalities differ across engines")
            write_json_new(META / "loaded_row_counts.json", loaded)
            databases = {"SQLite": sqlite_path, "DuckDB": duckdb_path, "PostgreSQL": None}
            capture_plans_and_metadata(databases)
            write_json_new(META / "procedural_work_diagnostics.json", diagnostics(duckdb_path, duckdb_counts))
            semantic = validate_semantics(databases, events)
            write_json_new(META / "semantic_validation.json", semantic)
            run_trials(databases, semantic, writer, raw_handle, events)
            completed = True
        finally:
            stop_postgres(postgres_process or STARTED_PG_PROCESS)
    if completed:
        write_json_new(META / "execution_complete.json", {
            "run_id": RUN_ID, "status": "COMPLETE", "udfbench_commit": COMMIT,
            "raw_trials": str(trials_path.relative_to(ROOT)),
            "postgres_stopped": STARTED_PG_PROCESS is None or STARTED_PG_PROCESS.poll() is not None,
            "elapsed_seconds": time.perf_counter() - overall_started,
        })
    print(json.dumps({"status": "COMPLETE", "run_id": RUN_ID, "raw_trials": str(trials_path)}, sort_keys=True))


if __name__ == "__main__":
    main()
