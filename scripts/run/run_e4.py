#!/usr/bin/env python3
"""Run preregistered E4 blocks on SQLite, DuckDB, or isolated PostgreSQL."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import platform
import signal
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from workloads.synthetic.e4_kernel import branch_loop_checksum_observed  # noqa: E402


R = 10_000
TIMEOUT_SECONDS = 60.0
CORRELATIONS = ("positive", "negative", "independent")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json_exclusive(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


class ExternalTimeout(Exception):
    pass


class Adapter:
    system: str
    version: str
    function_metadata: dict[str, Any]

    def load_high(self, datasets: dict[str, dict[str, Any]]) -> None:
        raise NotImplementedError

    def query(self, correlation: str) -> str:
        return f"SELECT COUNT(*), SUM(e4_branch_loop(value, flag, loop_count)) FROM e4_{correlation}"

    def explain(self, correlation: str) -> tuple[Any, float | None]:
        raise NotImplementedError

    def execute(self, correlation: str) -> dict[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class SQLiteAdapter(Adapter):
    system = "sqlite"

    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.version = sqlite3.sqlite_version
        self.calls = 0
        self.work = 0

        def udf(value: int, flag: int, iterations: int) -> int:
            self.calls += 1
            result, observed = branch_loop_checksum_observed(value, flag, iterations)
            self.work += observed
            return result

        self.connection.create_function("e4_branch_loop", 3, udf, deterministic=True)
        self.function_metadata = {
            "language": "Python sqlite3 scalar callback",
            "deterministic": True,
            "optimizer_cost": None,
            "side_effects": False,
        }

    def load_high(self, datasets: dict[str, dict[str, Any]]) -> None:
        for correlation, dataset in datasets.items():
            table = f"e4_{correlation}"
            self.connection.execute(f"DROP TABLE IF EXISTS {table}")
            self.connection.execute(
                f"CREATE TABLE {table} (id INTEGER, value INTEGER, flag INTEGER, loop_count INTEGER, retained INTEGER, lookup_key INTEGER)"
            )
            with Path(dataset["dataset_path"]).open(newline="", encoding="utf-8") as handle:
                rows = [tuple(int(row[key]) for key in ("id", "value", "flag", "loop_count", "retained", "lookup_key")) for row in csv.DictReader(handle)]
            self.connection.executemany(f"INSERT INTO {table} VALUES (?,?,?,?,?,?)", rows)
            self.connection.commit()

    def explain(self, correlation: str) -> tuple[Any, float | None]:
        rows = self.connection.execute("EXPLAIN QUERY PLAN " + self.query(correlation)).fetchall()
        return [list(row) for row in rows], None

    def execute(self, correlation: str) -> dict[str, Any]:
        self.calls = 0
        self.work = 0
        row_count, checksum = self.connection.execute(self.query(correlation)).fetchone()
        return {
            "row_count": int(row_count), "checksum": int(checksum),
            "udf_invocations": self.calls, "observed_work": self.work,
        }

    def close(self) -> None:
        self.connection.close()


class DuckDBAdapter(Adapter):
    system = "duckdb"

    def __init__(self) -> None:
        import duckdb
        from duckdb.sqltypes import BIGINT

        self.duckdb = duckdb
        self.connection = duckdb.connect(":memory:")
        self.version = duckdb.__version__
        self.calls = 0
        self.work = 0

        def udf(value: int, flag: int, iterations: int) -> int:
            self.calls += 1
            result, observed = branch_loop_checksum_observed(value, flag, iterations)
            self.work += observed
            return result

        self.connection.create_function(
            "e4_branch_loop", udf, [BIGINT, BIGINT, BIGINT], BIGINT, side_effects=False
        )
        self.function_metadata = {
            "language": "DuckDB native Python scalar callback",
            "deterministic": True,
            "optimizer_cost": None,
            "side_effects": False,
        }

    def load_high(self, datasets: dict[str, dict[str, Any]]) -> None:
        for correlation, dataset in datasets.items():
            table = f"e4_{correlation}"
            self.connection.execute(f"DROP TABLE IF EXISTS {table}")
            self.connection.execute(
                f"CREATE TABLE {table} AS SELECT id::BIGINT AS id, value::BIGINT AS value, "
                "flag::BIGINT AS flag, loop_count::BIGINT AS loop_count, retained::BIGINT AS retained, "
                "lookup_key::BIGINT AS lookup_key FROM read_csv_auto(?, header=true)",
                [dataset["dataset_path"]],
            )

    def explain(self, correlation: str) -> tuple[Any, float | None]:
        plan = self.connection.execute("EXPLAIN " + self.query(correlation)).fetchone()[1]
        return plan, None

    def execute(self, correlation: str) -> dict[str, Any]:
        self.calls = 0
        self.work = 0
        row_count, checksum = self.connection.execute(self.query(correlation)).fetchone()
        return {
            "row_count": int(row_count), "checksum": int(checksum),
            "udf_invocations": self.calls, "observed_work": self.work,
        }

    def close(self) -> None:
        self.connection.close()


PLPYTHON_BODY = r"""
if not flag:
    result = int(value)
    observed = 0
else:
    result = int(value) & 0x7FFFFFFF
    observed = 0
    for index in range(int(iterations)):
        result = (result * 1103515245 + 12345 + index) & 0x7FFFFFFF
        observed += 1
GD["e4_calls"] = GD.get("e4_calls", 0) + 1
GD["e4_work"] = GD.get("e4_work", 0) + observed
return result
""".strip()


class PostgreSQLAdapter(Adapter):
    system = "postgresql"

    def __init__(self, args: argparse.Namespace) -> None:
        import psycopg2

        self.psycopg2 = psycopg2
        self.connection = psycopg2.connect(
            host=args.pg_socket, port=args.pg_port, dbname=args.pg_database,
            user=args.pg_user, connect_timeout=5, application_name=f"survey-{args.run_id}-e4",
        )
        self.connection.autocommit = True
        with self.connection.cursor() as cursor:
            cursor.execute("SET statement_timeout='60000ms'")
            cursor.execute("SET jit=off")
            cursor.execute("SET max_parallel_workers_per_gather=0")
            cursor.execute(
                f"""
                CREATE OR REPLACE FUNCTION e4_branch_loop(value bigint, flag bigint, iterations bigint)
                RETURNS bigint LANGUAGE plpython3u IMMUTABLE STRICT PARALLEL UNSAFE COST 100
                AS $PYTHON$
{PLPYTHON_BODY}
$PYTHON$
                """
            )
            cursor.execute(
                """
                CREATE OR REPLACE FUNCTION e4_reset_counters() RETURNS void
                LANGUAGE plpython3u VOLATILE PARALLEL UNSAFE AS $PYTHON$
GD["e4_calls"] = 0
GD["e4_work"] = 0
return None
$PYTHON$
                """
            )
            cursor.execute(
                """
                CREATE OR REPLACE FUNCTION e4_read_counters() RETURNS bigint[]
                LANGUAGE plpython3u VOLATILE PARALLEL UNSAFE AS $PYTHON$
return [GD.get("e4_calls", 0), GD.get("e4_work", 0)]
$PYTHON$
                """
            )
            cursor.execute("SELECT version()")
            self.version = cursor.fetchone()[0]
            cursor.execute(
                """
                SELECT p.oid::text, l.lanname, p.provolatile, p.proparallel,
                       p.proisstrict, p.proleakproof, p.procost::float8,
                       pg_get_function_identity_arguments(p.oid)
                FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang
                WHERE p.oid='e4_branch_loop(bigint,bigint,bigint)'::regprocedure
                """
            )
            row = cursor.fetchone()
        self.function_metadata = {
            "oid": row[0], "language": row[1],
            "volatility": {"i": "immutable", "s": "stable", "v": "volatile"}[row[2]],
            "parallel_safety": {"s": "safe", "r": "restricted", "u": "unsafe"}[row[3]],
            "strict": row[4], "leakproof": row[5], "cost": row[6],
            "identity_arguments": row[7],
            "plpython_body_sha256": hashlib.sha256(PLPYTHON_BODY.encode("utf-8")).hexdigest(),
        }

    def load_high(self, datasets: dict[str, dict[str, Any]]) -> None:
        with self.connection.cursor() as cursor:
            for correlation, dataset in datasets.items():
                table = f"e4_{correlation}"
                cursor.execute(f"DROP TABLE IF EXISTS {table}")
                cursor.execute(
                    f"CREATE TABLE {table} (id bigint, value bigint, flag bigint, loop_count bigint, retained bigint, lookup_key bigint)"
                )
                with Path(dataset["dataset_path"]).open("r", encoding="utf-8", newline="") as handle:
                    cursor.copy_expert(
                        f"COPY {table} (id,value,flag,loop_count,retained,lookup_key) FROM STDIN WITH (FORMAT CSV, HEADER TRUE)",
                        handle,
                    )
                cursor.execute(f"ANALYZE {table}")

    def explain(self, correlation: str) -> tuple[Any, float | None]:
        with self.connection.cursor() as cursor:
            cursor.execute("EXPLAIN (VERBOSE TRUE, COSTS TRUE, FORMAT JSON) " + self.query(correlation))
            plan = cursor.fetchone()[0]
        return plan, float(plan[0]["Plan"]["Total Cost"])

    def execute(self, correlation: str) -> dict[str, Any]:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT e4_reset_counters()")
            cursor.execute(self.query(correlation))
            row_count, checksum = cursor.fetchone()
            cursor.execute("SELECT e4_read_counters()")
            counters = cursor.fetchone()[0]
        return {
            "row_count": int(row_count), "checksum": int(checksum),
            "udf_invocations": int(counters[0]), "observed_work": int(counters[1]),
        }

    def close(self) -> None:
        self.connection.close()


def timed_execute(adapter: Adapter, correlation: str) -> dict[str, Any]:
    previous = signal.getsignal(signal.SIGALRM)

    def expired(_signum: int, _frame: Any) -> None:
        raise ExternalTimeout("external E4 trial watchdog expired")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, TIMEOUT_SECONDS + 5)
    started = time.perf_counter_ns()
    try:
        result = adapter.execute(correlation)
        status, error = "ok", None
    except Exception as exc:
        result = {"row_count": None, "checksum": None, "udf_invocations": None, "observed_work": None}
        status = "timeout" if isinstance(exc, ExternalTimeout) or "statement timeout" in str(exc).lower() else "failed"
        error = repr(exc)
    finally:
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    return {**result, "wall_time_ms": elapsed_ms, "status": status, "error": error}


def create_adapter(args: argparse.Namespace) -> Adapter:
    if args.system == "sqlite":
        return SQLiteAdapter()
    if args.system == "duckdb":
        return DuckDBAdapter()
    return PostgreSQLAdapter(args)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--system", choices=("sqlite", "duckdb", "postgresql"), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--preregistration", type=Path, required=True)
    parser.add_argument("--pg-socket")
    parser.add_argument("--pg-port", type=int)
    parser.add_argument("--pg-database")
    parser.add_argument("--pg-user", default=os.environ.get("PGUSER") or os.environ.get("USER"))
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    prereg = json.loads(args.preregistration.read_text(encoding="utf-8"))
    if prereg["run_id"] != args.run_id or manifest["run_id"] != args.run_id:
        raise RuntimeError("run ID mismatch")
    dataset_index = {
        (item["H"], item["correlation"]): item for item in manifest["datasets"]
    }
    for item in manifest["datasets"]:
        if sha256(Path(item["dataset_path"])) != item["dataset_sha256"]:
            raise RuntimeError(f"frozen dataset changed: {item['dataset_path']}")

    raw_dir = ROOT / "results" / "raw" / args.run_id
    plan_dir = ROOT / "results" / "plans" / args.run_id
    metadata_dir = ROOT / "metadata" / args.run_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    plan_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"e4_{args.system}.jsonl"
    environment_path = metadata_dir / f"e4_{args.system}_environment.json"
    adapter = create_adapter(args)
    environment = {
        "run_id": args.run_id,
        "system": args.system,
        "version": adapter.version,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "function_metadata": adapter.function_metadata,
        "canonical_udf_source_sha256": prereg["canonical_udf_source_sha256"],
        "cache_state": "database_warm_os_uncontrolled",
        "captured_at_utc": utc_now(),
    }
    write_json_exclusive(environment_path, environment)

    plans: dict[tuple[int, str], dict[str, Any]] = {}
    current_high = None
    with raw_path.open("x", encoding="utf-8") as raw:
        try:
            for block in prereg["execution_schedule"]:
                high = int(block["H"])
                if current_high != high:
                    datasets = {correlation: dataset_index[(high, correlation)] for correlation in CORRELATIONS}
                    adapter.load_high(datasets)
                    for correlation in CORRELATIONS:
                        plan, estimated_cost = adapter.explain(correlation)
                        plan_path = plan_dir / f"e4_{args.system}_h{high}_{correlation}.json"
                        write_json_exclusive(plan_path, {
                            "run_id": args.run_id, "system": args.system, "H": high,
                            "correlation": correlation, "query": adapter.query(correlation),
                            "estimated_total_cost": estimated_cost, "plan": plan,
                        })
                        plans[(high, correlation)] = {
                            "path": str(plan_path.resolve()), "estimated_total_cost": estimated_cost,
                        }
                    current_high = high
                for position, correlation in enumerate(block["order"], start=1):
                    dataset = dataset_index[(high, correlation)]
                    result = timed_execute(adapter, correlation)
                    semantic_ok = (
                        result["status"] == "ok"
                        and result["row_count"] == R
                        and result["udf_invocations"] == R
                        and result["observed_work"] == dataset["true_work"]
                    )
                    status = result["status"] if result["status"] != "ok" or semantic_ok else "validation_failed"
                    record = {
                        "schema_version": "1.0.0",
                        "run_id": args.run_id,
                        "experiment_id": "E4",
                        "system": args.system,
                        "system_version": adapter.version,
                        "trial_id": f"{args.run_id}-{args.system}-{block['block_id']}-{correlation}",
                        "block_id": block["block_id"],
                        "execution_position": position,
                        "trial_phase": block["trial_phase"],
                        "trial_number": block["trial_number"],
                        "R": R,
                        "L": dataset["L"],
                        "H": high,
                        "correlation": correlation,
                        "seed": dataset["seed"],
                        "dataset_path": dataset["dataset_path"],
                        "dataset_sha256": dataset["dataset_sha256"],
                        "joint_cell_counts": dataset["joint_cell_counts"],
                        "p_z_active": dataset["p_z_active"],
                        "p_i_low": dataset["p_i_low"],
                        "p_i_high": dataset["p_i_high"],
                        "e_i": dataset["e_i"],
                        "e_i_given_z_active": dataset["e_i_given_z_active"],
                        "expected_udf_invocations": R,
                        "observed_udf_invocations": result["udf_invocations"],
                        "expected_work": dataset["true_work"],
                        "observed_work": result["observed_work"],
                        "m1_predicted_work": dataset["m1_predicted_work"],
                        "m1_qerror": dataset["m1_qerror"],
                        "m2_predicted_work": dataset["m2_predicted_work"],
                        "m2_qerror": dataset["m2_qerror"],
                        "row_count": result["row_count"],
                        "result_checksum": result["checksum"],
                        "wall_time_ms": result["wall_time_ms"],
                        "query": adapter.query(correlation),
                        "plan_path": plans[(high, correlation)]["path"],
                        "estimated_total_plan_cost": plans[(high, correlation)]["estimated_total_cost"],
                        "function_metadata": adapter.function_metadata,
                        "timeout_seconds": TIMEOUT_SECONDS,
                        "warmup_count": 3,
                        "repetition_count": 10,
                        "status": status,
                        "error": result["error"] if status == result["status"] else "semantic count/work validation failed",
                        "timestamp_utc": utc_now(),
                    }
                    raw.write(json.dumps(record, sort_keys=True) + "\n")
                    raw.flush()
                    os.fsync(raw.fileno())
                print(f"{args.system} {block['block_id']} complete", flush=True)
        finally:
            adapter.close()
    print(f"E4 {args.system} complete: {raw_path}", flush=True)


if __name__ == "__main__":
    main()
