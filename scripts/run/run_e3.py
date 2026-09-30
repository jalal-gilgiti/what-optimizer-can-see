#!/usr/bin/env python3
"""Run preregistered E3 blocks on SQLite, DuckDB, or isolated PostgreSQL."""

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
from workloads.synthetic.e3_kernel import dynamic_loop_checksum_observed  # noqa: E402

TIMEOUT_SECONDS = 60.0
QUERIES = ("Q_all", "Q_low", "Q_high")


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

    @staticmethod
    def query(query_id: str) -> str:
        predicate = {"Q_all": "", "Q_low": " WHERE population='LOW'", "Q_high": " WHERE population='HIGH'"}[query_id]
        return "SELECT COUNT(*), SUM(e3_dynamic_loop(value, loop_count)) FROM e3_data" + predicate

    def load(self, dataset_path: Path) -> None:
        raise NotImplementedError

    def explain(self, query_id: str) -> tuple[Any, float | None, Any]:
        raise NotImplementedError

    def execute(self, query_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class SQLiteAdapter(Adapter):
    system = "sqlite"

    def __init__(self, _args: argparse.Namespace) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.version = sqlite3.sqlite_version
        self.calls = 0
        self.work = 0

        def udf(value: int, iterations: int) -> int:
            self.calls += 1
            result, observed = dynamic_loop_checksum_observed(value, iterations)
            self.work += observed
            return result

        self.connection.create_function("e3_dynamic_loop", 2, udf, deterministic=True)
        self.function_metadata = {
            "language": "Python sqlite3 scalar callback", "deterministic": True,
            "optimizer_cost": None, "side_effects": False,
        }

    def load(self, dataset_path: Path) -> None:
        self.connection.execute("CREATE TABLE e3_data (id INTEGER, value INTEGER, population TEXT, loop_count INTEGER)")
        with dataset_path.open(newline="", encoding="utf-8") as handle:
            rows = [(int(r["id"]), int(r["value"]), r["population"], int(r["loop_count"])) for r in csv.DictReader(handle)]
        self.connection.executemany("INSERT INTO e3_data VALUES (?,?,?,?)", rows)
        self.connection.execute("CREATE INDEX e3_population_idx ON e3_data(population)")
        self.connection.execute("ANALYZE e3_data")
        self.connection.commit()

    def explain(self, query_id: str) -> tuple[Any, float | None, Any]:
        plan = [list(row) for row in self.connection.execute("EXPLAIN QUERY PLAN " + self.query(query_id)).fetchall()]
        stats = [list(row) for row in self.connection.execute("SELECT tbl,idx,stat FROM sqlite_stat1 WHERE tbl='e3_data'").fetchall()]
        return plan, None, stats

    def execute(self, query_id: str) -> dict[str, Any]:
        self.calls = self.work = 0
        count, checksum = self.connection.execute(self.query(query_id)).fetchone()
        return {"row_count": int(count), "checksum": int(checksum), "udf_invocations": self.calls, "observed_work": self.work}

    def close(self) -> None:
        self.connection.close()


class DuckDBAdapter(Adapter):
    system = "duckdb"

    def __init__(self, _args: argparse.Namespace) -> None:
        import duckdb
        from duckdb.sqltypes import BIGINT

        self.connection = duckdb.connect(":memory:")
        self.version = duckdb.__version__
        self.calls = 0
        self.work = 0

        def udf(value: int, iterations: int) -> int:
            self.calls += 1
            result, observed = dynamic_loop_checksum_observed(value, iterations)
            self.work += observed
            return result

        self.connection.create_function("e3_dynamic_loop", udf, [BIGINT, BIGINT], BIGINT, side_effects=False)
        self.function_metadata = {
            "language": "DuckDB native Python scalar callback", "deterministic": True,
            "optimizer_cost": None, "side_effects": False,
        }

    def load(self, dataset_path: Path) -> None:
        self.connection.execute(
            "CREATE TABLE e3_data AS SELECT id::BIGINT AS id, value::BIGINT AS value, "
            "population::VARCHAR AS population, loop_count::BIGINT AS loop_count "
            "FROM read_csv_auto(?, header=true)", [str(dataset_path)]
        )
        self.connection.execute("ANALYZE e3_data")

    def explain(self, query_id: str) -> tuple[Any, float | None, Any]:
        plan = self.connection.execute("EXPLAIN " + self.query(query_id)).fetchone()[1]
        stats = [list(row) for row in self.connection.execute("PRAGMA table_info('e3_data')").fetchall()]
        return plan, None, stats

    def execute(self, query_id: str) -> dict[str, Any]:
        self.calls = self.work = 0
        count, checksum = self.connection.execute(self.query(query_id)).fetchone()
        return {"row_count": int(count), "checksum": int(checksum), "udf_invocations": self.calls, "observed_work": self.work}

    def close(self) -> None:
        self.connection.close()


PLPYTHON_BODY = r"""
result = int(value) & 0x7FFFFFFF
observed = 0
for index in range(int(iterations)):
    result = (result * 1103515245 + 12345 + index) & 0x7FFFFFFF
    observed += 1
GD["e3_calls"] = GD.get("e3_calls", 0) + 1
GD["e3_work"] = GD.get("e3_work", 0) + observed
return result
""".strip()


class PostgreSQLAdapter(Adapter):
    system = "postgresql"

    def __init__(self, args: argparse.Namespace) -> None:
        import psycopg2

        self.connection = psycopg2.connect(
            host=args.pg_socket, port=args.pg_port, dbname=args.pg_database, user=args.pg_user,
            connect_timeout=5, application_name=f"survey-{args.run_id}-e3",
        )
        self.connection.autocommit = True
        with self.connection.cursor() as cursor:
            cursor.execute("SET statement_timeout='60000ms'")
            cursor.execute("SET jit=off")
            cursor.execute("SET max_parallel_workers_per_gather=0")
            cursor.execute(f"""
                CREATE OR REPLACE FUNCTION e3_dynamic_loop(value bigint, iterations bigint)
                RETURNS bigint LANGUAGE plpython3u IMMUTABLE STRICT PARALLEL UNSAFE COST 100
                AS $PYTHON$
{PLPYTHON_BODY}
$PYTHON$
            """)
            cursor.execute('''CREATE OR REPLACE FUNCTION e3_reset_counters() RETURNS void
                LANGUAGE plpython3u VOLATILE PARALLEL UNSAFE AS $PYTHON$
GD["e3_calls"] = 0
GD["e3_work"] = 0
return None
$PYTHON$''')
            cursor.execute('''CREATE OR REPLACE FUNCTION e3_read_counters() RETURNS bigint[]
                LANGUAGE plpython3u VOLATILE PARALLEL UNSAFE AS $PYTHON$
return [GD.get("e3_calls", 0), GD.get("e3_work", 0)]
$PYTHON$''')
            cursor.execute("SELECT version()")
            self.version = cursor.fetchone()[0]
            cursor.execute('''SELECT l.lanname,p.provolatile,p.proparallel,p.proisstrict,p.proleakproof,
                p.procost::float8,pg_get_function_identity_arguments(p.oid)
                FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang
                WHERE p.oid='e3_dynamic_loop(bigint,bigint)'::regprocedure''')
            row = cursor.fetchone()
        self.function_metadata = {
            "language": row[0], "volatility": {"i": "immutable", "s": "stable", "v": "volatile"}[row[1]],
            "parallel_safety": {"s": "safe", "r": "restricted", "u": "unsafe"}[row[2]],
            "strict": row[3], "leakproof": row[4], "cost": row[5], "identity_arguments": row[6],
            "plpython_body_sha256": hashlib.sha256(PLPYTHON_BODY.encode()).hexdigest(),
        }

    def load(self, dataset_path: Path) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("CREATE TABLE e3_data (id bigint, value bigint, population text, loop_count bigint)")
            with dataset_path.open("r", encoding="utf-8", newline="") as handle:
                cursor.copy_expert("COPY e3_data (id,value,population,loop_count) FROM STDIN WITH (FORMAT CSV, HEADER TRUE)", handle)
            cursor.execute("CREATE INDEX e3_population_idx ON e3_data(population)")
            cursor.execute("ANALYZE e3_data")

    def explain(self, query_id: str) -> tuple[Any, float | None, Any]:
        with self.connection.cursor() as cursor:
            cursor.execute("EXPLAIN (VERBOSE TRUE, COSTS TRUE, FORMAT JSON) " + self.query(query_id))
            plan = cursor.fetchone()[0]
            cursor.execute("SELECT attname,null_frac,n_distinct,most_common_vals::text,most_common_freqs::text FROM pg_stats WHERE tablename='e3_data' ORDER BY attname")
            stats = [list(row) for row in cursor.fetchall()]
        return plan, float(plan[0]["Plan"]["Total Cost"]), stats

    def execute(self, query_id: str) -> dict[str, Any]:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT e3_reset_counters()")
            cursor.execute(self.query(query_id))
            count, checksum = cursor.fetchone()
            cursor.execute("SELECT e3_read_counters()")
            calls, work = cursor.fetchone()[0]
        return {"row_count": int(count), "checksum": int(checksum), "udf_invocations": int(calls), "observed_work": int(work)}

    def close(self) -> None:
        self.connection.close()


def timed_execute(adapter: Adapter, query_id: str) -> dict[str, Any]:
    previous = signal.getsignal(signal.SIGALRM)

    def expired(_signum: int, _frame: Any) -> None:
        raise ExternalTimeout("external E3 trial watchdog expired")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, TIMEOUT_SECONDS + 5)
    started = time.perf_counter_ns()
    try:
        result = adapter.execute(query_id)
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
    if manifest["run_id"] != args.run_id or prereg["run_id"] != args.run_id:
        raise RuntimeError("run ID mismatch")
    dataset_path = Path(manifest["dataset_path"])
    if sha256(dataset_path) != manifest["dataset_sha256"]:
        raise RuntimeError("frozen E3 dataset changed")
    expected = {item["query_id"]: item for item in manifest["contexts"]}

    raw_dir = ROOT / "results/raw" / args.run_id
    plan_dir = ROOT / "results/plans" / args.run_id
    metadata_dir = ROOT / "metadata" / args.run_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    plan_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"e3_{args.system}.jsonl"
    environment_path = metadata_dir / f"e3_{args.system}_environment.json"
    adapter = {"sqlite": SQLiteAdapter, "duckdb": DuckDBAdapter, "postgresql": PostgreSQLAdapter}[args.system](args)
    try:
        adapter.load(dataset_path)
        write_json_exclusive(environment_path, {
            "run_id": args.run_id, "system": args.system, "version": adapter.version,
            "python": platform.python_version(), "platform": platform.platform(),
            "function_metadata": adapter.function_metadata,
            "canonical_udf_source_sha256": prereg["canonical_udf_source_sha256"],
            "cache_state": "database_warm_os_uncontrolled", "captured_at_utc": utc_now(),
        })
        plans = {}
        for query_id in QUERIES:
            plan, estimated_cost, stats_state = adapter.explain(query_id)
            path = plan_dir / f"e3_{args.system}_{query_id.lower()}.json"
            write_json_exclusive(path, {
                "run_id": args.run_id, "system": args.system, "query_id": query_id,
                "query": adapter.query(query_id), "estimated_total_cost": estimated_cost,
                "function_metadata": adapter.function_metadata, "statistics_state": stats_state, "plan": plan,
            })
            plans[query_id] = {"path": str(path.resolve()), "estimated_cost": estimated_cost}

        with raw_path.open("x", encoding="utf-8") as raw:
            for block in prereg["execution_schedule"]:
                for position, query_id in enumerate(block["order"], start=1):
                    expectation = expected[query_id]
                    result = timed_execute(adapter, query_id)
                    semantic_ok = (
                        result["status"] == "ok"
                        and result["row_count"] == expectation["expected_rows"]
                        and result["udf_invocations"] == expectation["expected_rows"]
                        and result["observed_work"] == expectation["true_work"]
                    )
                    status = result["status"] if result["status"] != "ok" or semantic_ok else "validation_failed"
                    record = {
                        "schema_version": "1.0.0", "run_id": args.run_id, "experiment_id": "E3",
                        "system": args.system, "system_version": adapter.version,
                        "trial_id": f"{args.run_id}-{args.system}-{block['block_id']}-{query_id}",
                        "block_id": block["block_id"], "execution_position": position,
                        "trial_phase": block["trial_phase"], "trial_number": block["trial_number"],
                        "query_id": query_id, "predicate_population": expectation["predicate_population"],
                        "R": manifest["R"], "selected_rows_expected": expectation["expected_rows"],
                        "selected_rows_observed": result["row_count"], "selectivity": expectation["selectivity"],
                        "dataset_path": str(dataset_path.resolve()), "dataset_sha256": manifest["dataset_sha256"],
                        "global_e_i": expectation["e_i_global"], "conditioned_e_i": expectation["e_i_given_query"],
                        "expected_udf_invocations": expectation["expected_rows"],
                        "observed_udf_invocations": result["udf_invocations"],
                        "expected_work": expectation["true_work"], "observed_work": result["observed_work"],
                        "m1_global_predicted_work": expectation["m1_global_predicted_work"],
                        "m1_global_qerror": expectation["m1_global_qerror"],
                        "m2_conditioned_predicted_work": expectation["m2_conditioned_predicted_work"],
                        "m2_conditioned_qerror": expectation["m2_conditioned_qerror"],
                        "result_checksum": result["checksum"], "wall_time_ms": result["wall_time_ms"],
                        "query": adapter.query(query_id), "plan_path": plans[query_id]["path"],
                        "estimated_total_plan_cost": plans[query_id]["estimated_cost"],
                        "function_metadata": adapter.function_metadata, "timeout_seconds": TIMEOUT_SECONDS,
                        "warmup_count": 3, "repetition_count": 10, "status": status,
                        "error": result["error"] if status == result["status"] else "semantic count/work validation failed",
                        "timestamp_utc": utc_now(),
                    }
                    raw.write(json.dumps(record, sort_keys=True) + "\n")
                    raw.flush()
                    os.fsync(raw.fileno())
                print(f"{args.system} {block['block_id']} complete", flush=True)
    finally:
        adapter.close()
    print(f"E3 {args.system} complete: {raw_path}", flush=True)


if __name__ == "__main__":
    main()
