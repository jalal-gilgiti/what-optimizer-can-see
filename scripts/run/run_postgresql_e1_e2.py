#!/usr/bin/env python3
"""Run the frozen PostgreSQL E1/E2 replication and scalar-COST probe."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import platform
import signal
import sys
import time
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extensions import connection as Connection


ROOT = Path(__file__).resolve().parents[2]
E1_ROWS = (100, 1_000, 10_000)
E1_LOOPS = ((1,), (10,), (10, 10), (8, 10, 12), (10, 10, 20))
E2_ROWS = 10_000
E2_LOOPS = E1_LOOPS[:-1]
E2_RHOS = (1.0, 0.5, 0.1, 0.01)
E2_FORMS = ("udf_before_filter", "filter_before_udf")
WARMUPS = 3
REPETITIONS = 10
SEED = 42
TIMEOUT_SECONDS = 60.0


PLPYTHON_BODY = r"""
bounds_list = [int(item) for item in bounds]
state = int(value) & 0x7FFFFFFF
observed_iterations = 0

def visit(depth):
    nonlocal state, observed_iterations
    if depth == len(bounds_list):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        observed_iterations += 1
        return
    for index in range(bounds_list[depth]):
        state = (state + index + depth) & 0x7FFFFFFF
        visit(depth + 1)

visit(0)
GD["survey_udf_invocations"] = GD.get("survey_udf_invocations", 0) + 1
GD["survey_loop_iterations"] = GD.get("survey_loop_iterations", 0) + observed_iterations
return state
""".strip()


class ExternalTimeout(Exception):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def amplification(bounds: tuple[int, ...]) -> int:
    result = 1
    for bound in bounds:
        result *= bound
    return result


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def dataset_path(family: str, rows: int, rho: float) -> Path:
    rho_label = str(rho).replace(".", "p")
    return ROOT / "workloads" / "synthetic" / "generated" / f"{family}_r{rows}_rho{rho_label}_seed42.csv"


def validate_dataset(path: Path, rows: int, rho: float) -> dict[str, Any]:
    metadata_path = path.with_suffix(path.suffix + ".metadata.json")
    if not path.is_file() or not metadata_path.is_file():
        raise FileNotFoundError(f"frozen dataset or metadata missing: {path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    observed_hash = sha256_path(path)
    if observed_hash != metadata["dataset_sha256"]:
        raise RuntimeError(f"frozen dataset checksum mismatch: {path}")
    if metadata["row_count"] != rows or metadata["seed"] != SEED:
        raise RuntimeError(f"frozen dataset cardinality/seed mismatch: {path}")
    if float(metadata["filter_selectivity_realized"]) != rho:
        raise RuntimeError(f"frozen dataset selectivity mismatch: {path}")
    return {"path": str(path.resolve()), "sha256": observed_hash, "metadata": metadata}


def connect(args: argparse.Namespace) -> Connection:
    connection = psycopg2.connect(
        host=args.socket,
        port=args.port,
        dbname=args.database,
        user=args.user,
        connect_timeout=5,
        application_name=f"survey-{args.run_id}",
    )
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = %s", (f"{int(TIMEOUT_SECONDS * 1000)}ms",))
        cursor.execute("SET lock_timeout = '5s'")
        cursor.execute("SET idle_in_transaction_session_timeout = '60s'")
        cursor.execute("SET jit = off")
        cursor.execute("SET max_parallel_workers_per_gather = 0")
    return connection


def install_schema(connection: Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS input_rows (
                id bigint NOT NULL,
                value bigint NOT NULL,
                flag bigint NOT NULL,
                loop_count bigint NOT NULL,
                retained bigint NOT NULL,
                lookup_key bigint NOT NULL
            )
            """
        )
        cursor.execute(
            f"""
            CREATE OR REPLACE FUNCTION fixed_loop(value bigint, bounds integer[])
            RETURNS bigint
            LANGUAGE plpython3u
            IMMUTABLE STRICT PARALLEL UNSAFE
            COST 100
            AS $PYTHON$
{PLPYTHON_BODY}
$PYTHON$
            """
        )
        cursor.execute(
            """
            CREATE OR REPLACE FUNCTION reset_fixed_loop_counters()
            RETURNS void
            LANGUAGE plpython3u VOLATILE PARALLEL UNSAFE
            AS $PYTHON$
GD["survey_udf_invocations"] = 0
GD["survey_loop_iterations"] = 0
return None
$PYTHON$
            """
        )
        cursor.execute(
            """
            CREATE OR REPLACE FUNCTION read_fixed_loop_counters()
            RETURNS bigint[]
            LANGUAGE plpython3u VOLATILE PARALLEL UNSAFE
            AS $PYTHON$
return [GD.get("survey_udf_invocations", 0), GD.get("survey_loop_iterations", 0)]
$PYTHON$
            """
        )


def function_metadata(connection: Connection) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT p.oid::text, l.lanname, p.provolatile, p.proparallel,
                   p.proisstrict, p.proleakproof, p.prosecdef,
                   p.procost::float8, p.prorows::float8,
                   pg_get_function_identity_arguments(p.oid),
                   pg_get_functiondef(p.oid)
            FROM pg_proc AS p
            JOIN pg_language AS l ON l.oid = p.prolang
            WHERE p.oid = 'fixed_loop(bigint,integer[])'::regprocedure
            """
        )
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("fixed_loop catalog row missing")
    volatility = {"i": "immutable", "s": "stable", "v": "volatile"}[row[2]]
    parallel = {"s": "safe", "r": "restricted", "u": "unsafe"}[row[3]]
    return {
        "oid": row[0],
        "language": row[1],
        "volatility": volatility,
        "parallel_safety": parallel,
        "strict": row[4],
        "leakproof": row[5],
        "security_definer": row[6],
        "cost": row[7],
        "rows": row[8],
        "identity_arguments": row[9],
        "catalog_definition_sha256": hashlib.sha256(row[10].encode("utf-8")).hexdigest(),
        "logical_body_sha256": hashlib.sha256(PLPYTHON_BODY.encode("utf-8")).hexdigest(),
    }


def load_dataset(connection: Connection, dataset: dict[str, Any]) -> None:
    with connection.cursor() as cursor:
        cursor.execute("TRUNCATE input_rows")
        with Path(dataset["path"]).open("r", encoding="utf-8", newline="") as handle:
            cursor.copy_expert(
                "COPY input_rows (id,value,flag,loop_count,retained,lookup_key) FROM STDIN WITH (FORMAT CSV, HEADER TRUE)",
                handle,
            )
        cursor.execute("ANALYZE input_rows")
        cursor.execute("SELECT count(*), sum((retained = 1)::int) FROM input_rows")
        observed_rows, observed_retained = cursor.fetchone()
    expected_rows = dataset["metadata"]["row_count"]
    expected_retained = round(expected_rows * dataset["metadata"]["filter_selectivity_realized"])
    if (observed_rows, observed_retained) != (expected_rows, expected_retained):
        raise RuntimeError(
            f"loaded cardinality mismatch: got {(observed_rows, observed_retained)}, "
            f"expected {(expected_rows, expected_retained)}"
        )


def bounds_sql(bounds: tuple[int, ...]) -> str:
    return "ARRAY[" + ",".join(str(value) for value in bounds) + "]::integer[]"


def query_for(bounds: tuple[int, ...], form: str | None, natural: bool = False) -> str:
    udf = f"fixed_loop(value, {bounds_sql(bounds)})"
    if form is None:
        return f"SELECT COUNT(*), SUM({udf}) FROM input_rows"
    if form == "filter_before_udf":
        return f"SELECT COUNT(*), SUM({udf}) FROM input_rows WHERE retained = 1"
    if form != "udf_before_filter":
        raise ValueError(form)
    if natural:
        return (
            "SELECT COUNT(*), SUM(udf_value) FROM ("
            f"SELECT retained, {udf} AS udf_value FROM input_rows"
            ") AS projected WHERE retained = 1"
        )
    return (
        "WITH projected AS MATERIALIZED ("
        f"SELECT retained, {udf} AS udf_value FROM input_rows"
        ") SELECT COUNT(*), SUM(udf_value) FROM projected WHERE retained = 1"
    )


def explain(connection: Connection, query: str) -> tuple[Any, float]:
    started = time.perf_counter_ns()
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN (VERBOSE TRUE, COSTS TRUE, FORMAT JSON) " + query)
        plan = cursor.fetchone()[0]
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    return plan, elapsed_ms


def plan_total_cost(plan: Any) -> float | None:
    try:
        return float(plan[0]["Plan"]["Total Cost"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def plan_node_types(plan: Any) -> list[str]:
    output: list[str] = []

    def visit(node: dict[str, Any]) -> None:
        output.append(str(node.get("Node Type")))
        for child in node.get("Plans", []):
            visit(child)

    try:
        visit(plan[0]["Plan"])
    except (KeyError, IndexError, TypeError):
        pass
    return output


def run_query(connection: Connection, query: str) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT reset_fixed_loop_counters()")

    previous = signal.getsignal(signal.SIGALRM)

    def expired(_signum: int, _frame: Any) -> None:
        raise ExternalTimeout(f"external watchdog exceeded {TIMEOUT_SECONDS + 5:.0f}s")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, TIMEOUT_SECONDS + 5)
    started = time.perf_counter_ns()
    status = "ok"
    error = None
    row_count = None
    checksum = None
    try:
        with connection.cursor() as cursor:
            cursor.execute(query)
            row_count, checksum = cursor.fetchone()
    except Exception as exc:  # Preserve all PostgreSQL/timeout failures.
        status = "timeout" if isinstance(exc, ExternalTimeout) or "statement timeout" in str(exc).lower() else "failed"
        error = repr(exc)
    finally:
        wall_time_ms = (time.perf_counter_ns() - started) / 1_000_000
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

    invocations = None
    iterations = None
    if status == "ok":
        with connection.cursor() as cursor:
            cursor.execute("SELECT read_fixed_loop_counters()")
            counters = cursor.fetchone()[0]
            invocations, iterations = int(counters[0]), int(counters[1])
    return {
        "status": status,
        "error": error,
        "wall_time_ms": wall_time_ms,
        "row_count": int(row_count) if row_count is not None else None,
        "checksum": int(checksum) if checksum is not None else None,
        "udf_invocations": invocations,
        "loop_iterations": iterations,
    }


def write_json_exclusive(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def write_plan_exclusive(path: Path, query: str, plan: Any, explain_wall_ms: float) -> None:
    write_json_exclusive(
        path,
        {"query": query, "explain_wall_ms": explain_wall_ms, "plan": plan},
    )


def append_record(handle: Any, record: dict[str, Any]) -> None:
    handle.write(json.dumps(record, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def base_record(
    args: argparse.Namespace,
    family: str,
    rows: int,
    rho: float,
    bounds: tuple[int, ...],
    form: str | None,
    dataset: dict[str, Any],
    phase: str,
    trial_number: int,
    query: str,
    plan_path: Path,
    expected_invocations: int,
    result: dict[str, Any],
    metadata: dict[str, Any],
    natural_classification: str | None = None,
    scalar_cost: float | None = None,
) -> dict[str, Any]:
    lambda_value = amplification(bounds)
    form_label = form or "single_form"
    configuration = {
        "system": "postgresql",
        "family": family,
        "R": rows,
        "rho": rho,
        "Lambda": lambda_value,
        "loop_bounds": list(bounds),
        "form": form,
        "function_cost": scalar_cost if scalar_cost is not None else metadata["cost"],
        "dataset_sha256": dataset["sha256"],
        "seed": SEED,
    }
    config_id = f"{family.lower()}-postgresql-r{rows}-rho{rho:g}-l{lambda_value}-{form_label}"
    expected_iterations = expected_invocations * lambda_value
    semantic_ok = (
        result["status"] == "ok"
        and result["udf_invocations"] == expected_invocations
        and result["loop_iterations"] == expected_iterations
        and result["row_count"] == round(rows * rho)
    )
    status = result["status"] if result["status"] != "ok" or semantic_ok else "validation_failed"
    return {
        "schema_version": "1.2.0",
        "experiment_family": family,
        "experiment_id": config_id,
        "configuration_id": config_id,
        "configuration_hash": stable_hash(configuration),
        "run_id": args.run_id,
        "trial_id": f"{args.run_id}-{config_id}-{phase}-{trial_number}",
        "trial_phase": phase,
        "trial_number": trial_number,
        "system": "postgresql",
        "system_version": "14.24",
        "language_runtime": platform.python_version(),
        "workload": "fixed_loop" if family == "E1" else "relational_selectivity_fixed_loop",
        "udf_id": "fixed_loop_checksum_observed",
        "udf_type": "pure_deterministic_plpython3u_with_backend_local_counters",
        "R": rows,
        "rho": rho,
        "form": form,
        "seed": SEED,
        "warmup_count": WARMUPS,
        "repetition_count": REPETITIONS,
        "timeout_seconds": TIMEOUT_SECONDS,
        "cache_state": "database_warm_os_uncontrolled",
        "timestamp_utc": utc_now(),
        "dataset_sha256": dataset["sha256"],
        "dataset_path": dataset["path"],
        "procedural_parameters": {
            "Lambda": lambda_value,
            "loop_bounds": list(bounds),
            "loop_depth": len(bounds),
            "loop_type": "fixed_nested",
        },
        "relational_parameters": {
            "input_cardinality": rows,
            "filter_selectivity_requested": rho,
            "filter_selectivity_realized": dataset["metadata"]["filter_selectivity_realized"],
        },
        "function_metadata": metadata,
        "natural_plan_classification": natural_classification,
        "decisions": {
            "evaluation_barrier": "materialized_cte" if form == "udf_before_filter" and family == "E2" else "none",
            "plan_regime": "controlled_counterfactual" if form == "udf_before_filter" and family == "E2" else "natural",
        },
        "measurements": {
            "wall_time_ms": result["wall_time_ms"],
            "database_execution_time_ms": None,
            "planning_time_ms": None,
            "expected_udf_invocations": expected_invocations,
            "udf_invocations": result["udf_invocations"],
            "rows_entering_udf": result["udf_invocations"],
            "expected_loop_iterations": expected_iterations,
            "observed_loop_iterations": result["loop_iterations"],
            "effective_loop_iterations": result["loop_iterations"],
            "rows_after_filter": result["row_count"],
            "result_checksum": result["checksum"],
            "estimated_cost": None,
            "query_text": query,
            "plan_path": str(plan_path.resolve()),
        },
        "status": status,
        "error": result["error"] if status == result["status"] else (
            f"semantic mismatch: expected calls={expected_invocations}, iterations={expected_iterations}, "
            f"rows={round(rows * rho)}; observed calls={result['udf_invocations']}, "
            f"iterations={result['loop_iterations']}, rows={result['row_count']}"
        ),
    }


def save_environment(args: argparse.Namespace, connection: Connection, datasets: list[dict[str, Any]]) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT version(), current_setting('server_encoding'), current_setting('lc_collate'), current_setting('lc_ctype')")
        version, encoding, collate, ctype = cursor.fetchone()
        settings = {}
        for name in (
            "shared_buffers", "work_mem", "effective_cache_size", "max_parallel_workers",
            "max_parallel_workers_per_gather", "jit", "random_page_cost", "cpu_tuple_cost",
            "cpu_operator_cost", "port", "unix_socket_directories", "listen_addresses", "TimeZone",
        ):
            cursor.execute("SELECT current_setting(%s)", (name,))
            settings[name] = cursor.fetchone()[0]
        cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'plpython3u'")
        plpython_extension_version = cursor.fetchone()[0]
        cursor.execute(
            """
            CREATE OR REPLACE FUNCTION survey_python_version() RETURNS text
            LANGUAGE plpython3u AS $PYTHON$
import sys
return sys.version
$PYTHON$
            """
        )
        cursor.execute("SELECT survey_python_version()")
        plpython_python = cursor.fetchone()[0]
        cursor.execute("DROP FUNCTION survey_python_version()")

    package_dir = Path(args.package_dir)
    packages = []
    for path in sorted(package_dir.glob("*.deb")):
        packages.append({"filename": path.name, "sha256": sha256_path(path)})
    payload = {
        "run_id": args.run_id,
        "classification": "ISOLATED_LOCAL_SETUP",
        "captured_at_utc": utc_now(),
        "postgresql_version": version,
        "psycopg2_version": psycopg2.__version__,
        "libpq_version": psycopg2.__libpq_version__,
        "plpython_extension_version": plpython_extension_version,
        "plpython_python_version": plpython_python,
        "database": args.database,
        "user": args.user,
        "socket_directory": args.socket,
        "port": args.port,
        "tcp_disabled": True,
        "encoding": encoding,
        "locale_collate": collate,
        "locale_ctype": ctype,
        "settings": settings,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "package_deployment": "Ubuntu .deb payloads extracted project-locally; maintainer scripts not executed",
        "package_files": packages,
        "datasets": [{"path": item["path"], "sha256": item["sha256"]} for item in datasets],
    }
    write_json_exclusive(Path(args.metadata_dir) / "postgresql_environment.json", payload)


def natural_probe(
    args: argparse.Namespace,
    connection: Connection,
    datasets_by_rho: dict[float, dict[str, Any]],
    plan_dir: Path,
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for rho in E2_RHOS:
        load_dataset(connection, datasets_by_rho[rho])
        retained = round(E2_ROWS * rho)
        for bounds in E2_LOOPS:
            lambda_value = amplification(bounds)
            pair: dict[str, Any] = {}
            for form in E2_FORMS:
                query = query_for(bounds, form, natural=True)
                plan, explain_ms = explain(connection, query)
                plan_path = plan_dir / f"natural-rho{rho:g}-l{lambda_value}-{form}.json"
                write_plan_exclusive(plan_path, query, plan, explain_ms)
                result = run_query(connection, query)
                pair[form] = {
                    "query": query,
                    "plan_path": str(plan_path.resolve()),
                    "plan_node_types": plan_node_types(plan),
                    "estimated_total_cost": plan_total_cost(plan),
                    "diagnostic_result": result,
                }
            a_calls = pair["udf_before_filter"]["diagnostic_result"]["udf_invocations"]
            b_calls = pair["filter_before_udf"]["diagnostic_result"]["udf_invocations"]
            checksums = {
                pair[form]["diagnostic_result"]["checksum"] for form in E2_FORMS
                if pair[form]["diagnostic_result"]["status"] == "ok"
            }
            if len(checksums) != 1:
                classification = "UNCERTAIN"
            elif a_calls == b_calls == retained:
                classification = "PHYSICALLY_EQUIVALENT"
            elif a_calls == E2_ROWS and b_calls == retained:
                classification = "NATURALLY_DISTINCT"
            elif a_calls == b_calls:
                classification = "OPTIMIZER_REORDERED"
            else:
                classification = "UNCERTAIN"
            pair["classification"] = classification
            pair["natural_udf_before_rewritten_to_filter_first"] = (
                rho < 1.0 and a_calls == retained
            )
            output[f"rho={rho:g},Lambda={lambda_value}"] = pair
            print(
                f"natural probe rho={rho:g} Lambda={lambda_value}: {classification}, "
                f"calls A/B={a_calls}/{b_calls}",
                flush=True,
            )
    return output


def run_e1(
    args: argparse.Namespace,
    connection: Connection,
    datasets_by_rows: dict[int, dict[str, Any]],
    metadata: dict[str, Any],
    raw_path: Path,
    plan_dir: Path,
) -> None:
    with raw_path.open("x", encoding="utf-8") as raw:
        for rows in E1_ROWS:
            dataset = datasets_by_rows[rows]
            load_dataset(connection, dataset)
            for bounds in E1_LOOPS:
                lambda_value = amplification(bounds)
                query = query_for(bounds, None)
                plan, explain_ms = explain(connection, query)
                plan_path = plan_dir / f"e1-r{rows}-l{lambda_value}.json"
                write_plan_exclusive(plan_path, query, plan, explain_ms)
                for phase, count in (("warmup", WARMUPS), ("measurement", REPETITIONS)):
                    for trial in range(1, count + 1):
                        result = run_query(connection, query)
                        record = base_record(
                            args, "E1", rows, 1.0, bounds, None, dataset, phase, trial,
                            query, plan_path, rows, result, metadata,
                        )
                        append_record(raw, record)
                print(f"E1 R={rows} Lambda={lambda_value} complete", flush=True)


def run_e2(
    args: argparse.Namespace,
    connection: Connection,
    datasets_by_rho: dict[float, dict[str, Any]],
    metadata: dict[str, Any],
    natural: dict[str, Any],
    raw_path: Path,
    plan_dir: Path,
) -> None:
    with raw_path.open("x", encoding="utf-8") as raw:
        for rho in E2_RHOS:
            dataset = datasets_by_rho[rho]
            load_dataset(connection, dataset)
            retained = round(E2_ROWS * rho)
            for bounds in E2_LOOPS:
                lambda_value = amplification(bounds)
                classification = natural[f"rho={rho:g},Lambda={lambda_value}"]["classification"]
                checksums_by_trial: dict[tuple[str, int], int | None] = {}
                for form in E2_FORMS:
                    query = query_for(bounds, form, natural=False)
                    plan, explain_ms = explain(connection, query)
                    plan_path = plan_dir / f"controlled-rho{rho:g}-l{lambda_value}-{form}.json"
                    write_plan_exclusive(plan_path, query, plan, explain_ms)
                    expected_invocations = E2_ROWS if form == "udf_before_filter" else retained
                    for phase, count in (("warmup", WARMUPS), ("measurement", REPETITIONS)):
                        for trial in range(1, count + 1):
                            result = run_query(connection, query)
                            checksums_by_trial[(phase, trial, form)] = result["checksum"]
                            record = base_record(
                                args, "E2", E2_ROWS, rho, bounds, form, dataset, phase, trial,
                                query, plan_path, expected_invocations, result, metadata,
                                natural_classification=classification,
                            )
                            append_record(raw, record)
                for phase, count in (("warmup", WARMUPS), ("measurement", REPETITIONS)):
                    for trial in range(1, count + 1):
                        if checksums_by_trial[(phase, trial, E2_FORMS[0])] != checksums_by_trial[(phase, trial, E2_FORMS[1])]:
                            raise RuntimeError(
                                f"E2 checksum mismatch rho={rho:g}, Lambda={lambda_value}, {phase}={trial}"
                            )
                print(
                    f"E2 rho={rho:g} Lambda={lambda_value} controlled pair complete "
                    f"(natural={classification})",
                    flush=True,
                )


def alter_cost(connection: Connection, value: float) -> None:
    with connection.cursor() as cursor:
        cursor.execute(f"ALTER FUNCTION fixed_loop(bigint, integer[]) COST {value:.12g}")


def run_scalar_cost(
    args: argparse.Namespace,
    admin: Connection,
    datasets_by_rho: dict[float, dict[str, Any]],
    baseline_metadata: dict[str, Any],
    raw_path: Path,
    plan_dir: Path,
    prereg_path: Path,
) -> None:
    baseline_cost = float(baseline_metadata["cost"])
    cost_values = (0.1 * baseline_cost, baseline_cost, 10.0 * baseline_cost, 100.0 * baseline_cost)
    selected_lambdas = (1, 100, 960)
    selected_rhos = (0.5, 0.1, 0.01)
    selected_datasets = [datasets_by_rho[rho] for rho in selected_rhos]
    preregistration = {
        "run_id": args.run_id,
        "timestamp_utc": utc_now(),
        "baseline_cost": baseline_cost,
        "cost_values": list(cost_values),
        "Lambda": list(selected_lambdas),
        "rho": list(selected_rhos),
        "R": E2_ROWS,
        "function_body_sha256": baseline_metadata["logical_body_sha256"],
        "dataset_checksums": {str(rho): datasets_by_rho[rho]["sha256"] for rho in selected_rhos},
        "configuration_hash": stable_hash({
            "cost_values": cost_values,
            "Lambda": selected_lambdas,
            "rho": selected_rhos,
            "R": E2_ROWS,
            "function_body_sha256": baseline_metadata["logical_body_sha256"],
        }),
        "execution_order": "rho, Lambda, COST ascending as listed; fixed before outcomes",
        "warmups": WARMUPS,
        "repetitions": REPETITIONS,
    }
    write_json_exclusive(prereg_path, preregistration)
    print(f"scalar COST preregistered: {list(cost_values)}", flush=True)

    bounds_index = {amplification(bounds): bounds for bounds in E2_LOOPS}
    try:
        with raw_path.open("x", encoding="utf-8") as raw:
            for rho in selected_rhos:
                load_dataset(admin, datasets_by_rho[rho])
                for lambda_value in selected_lambdas:
                    bounds = bounds_index[lambda_value]
                    query = query_for(bounds, "udf_before_filter", natural=True)
                    for cost in cost_values:
                        alter_cost(admin, cost)
                        observed_metadata = function_metadata(admin)
                        if float(observed_metadata["cost"]) != float(cost):
                            raise RuntimeError(f"catalog COST mismatch: expected {cost}, got {observed_metadata['cost']}")
                        for key in (
                            "language", "volatility", "parallel_safety", "strict", "leakproof",
                            "security_definer", "logical_body_sha256",
                        ):
                            if observed_metadata[key] != baseline_metadata[key]:
                                raise RuntimeError(f"function metadata changed unexpectedly: {key}")

                        fresh = connect(args)
                        try:
                            plan, explain_ms = explain(fresh, query)
                            cost_label = str(cost).replace(".", "p")
                            plan_path = plan_dir / f"cost-rho{rho:g}-l{lambda_value}-cost{cost_label}.json"
                            write_plan_exclusive(plan_path, query, plan, explain_ms)
                            retained = round(E2_ROWS * rho)
                            for phase, count in (("warmup", WARMUPS), ("measurement", REPETITIONS)):
                                for trial in range(1, count + 1):
                                    result = run_query(fresh, query)
                                    observed_calls = result["udf_invocations"]
                                    placement = (
                                        "filter_before_udf" if observed_calls == retained
                                        else "udf_before_filter" if observed_calls == E2_ROWS
                                        else "uncertain"
                                    )
                                    record = {
                                        "schema_version": "1.0.0",
                                        "experiment_family": "POSTGRESQL_SCALAR_COST",
                                        "run_id": args.run_id,
                                        "trial_id": (
                                            f"{args.run_id}-cost-rho{rho:g}-l{lambda_value}-cost{cost:g}-"
                                            f"{phase}-{trial}"
                                        ),
                                        "trial_phase": phase,
                                        "trial_number": trial,
                                        "R": E2_ROWS,
                                        "rho": rho,
                                        "Lambda": lambda_value,
                                        "loop_bounds": list(bounds),
                                        "function_cost": cost,
                                        "function_metadata": observed_metadata,
                                        "function_body_sha256": observed_metadata["logical_body_sha256"],
                                        "dataset_path": datasets_by_rho[rho]["path"],
                                        "dataset_sha256": datasets_by_rho[rho]["sha256"],
                                        "query_text": query,
                                        "plan_path": str(plan_path.resolve()),
                                        "plan_node_types": plan_node_types(plan),
                                        "estimated_total_plan_cost": plan_total_cost(plan),
                                        "observed_placement": placement,
                                        "expected_retained_rows": retained,
                                        "row_count": result["row_count"],
                                        "result_checksum": result["checksum"],
                                        "udf_invocations": observed_calls,
                                        "observed_loop_iterations": result["loop_iterations"],
                                        "wall_time_ms": result["wall_time_ms"],
                                        "status": result["status"],
                                        "error": result["error"],
                                        "timestamp_utc": utc_now(),
                                    }
                                    append_record(raw, record)
                        finally:
                            fresh.close()
                        print(
                            f"scalar COST rho={rho:g} Lambda={lambda_value} COST={cost:g} complete",
                            flush=True,
                        )
    finally:
        alter_cost(admin, baseline_cost)
        restored = function_metadata(admin)
        if float(restored["cost"]) != baseline_cost:
            raise RuntimeError("failed to restore baseline function COST")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--database", default="survey_experiments")
    parser.add_argument("--user", default=os.environ.get("PGUSER") or os.environ.get("USER"))
    parser.add_argument("--package-dir", required=True)
    parser.add_argument("--smoke-only", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.smoke_only:
        dataset = validate_dataset(dataset_path("fixed_loop", 100, 1.0), 100, 1.0)
        connection = connect(args)
        try:
            install_schema(connection)
            load_dataset(connection, dataset)
            result = run_query(connection, query_for((10,), None))
            if (
                result["status"] != "ok"
                or result["udf_invocations"] != 100
                or result["loop_iterations"] != 1_000
                or result["row_count"] != 100
            ):
                raise RuntimeError(f"PostgreSQL smoke validation failed: {result}")
            print(json.dumps({"smoke": "PASS", "result": result}, sort_keys=True))
        finally:
            connection.close()
        return

    raw_dir = ROOT / "results" / "raw" / args.run_id
    normalized_dir = ROOT / "results" / "normalized" / args.run_id
    plan_dir = ROOT / "results" / "plans" / args.run_id
    metadata_dir = ROOT / "metadata" / args.run_id
    for directory in (raw_dir, normalized_dir, plan_dir, metadata_dir):
        directory.mkdir(parents=True, exist_ok=False)
    args.metadata_dir = str(metadata_dir)

    e1_datasets = {
        rows: validate_dataset(dataset_path("fixed_loop", rows, 1.0), rows, 1.0)
        for rows in E1_ROWS
    }
    e2_datasets = {
        rho: validate_dataset(dataset_path("relational_selectivity", E2_ROWS, rho), E2_ROWS, rho)
        for rho in E2_RHOS
    }
    all_datasets = list(e1_datasets.values()) + list(e2_datasets.values())

    connection = connect(args)
    try:
        install_schema(connection)
        baseline_metadata = function_metadata(connection)
        write_json_exclusive(metadata_dir / "postgresql_function_metadata.json", baseline_metadata)
        save_environment(args, connection, all_datasets)

        natural = natural_probe(args, connection, e2_datasets, plan_dir)
        write_json_exclusive(metadata_dir / "postgresql_natural_placement_probe.json", natural)

        run_e1(
            args, connection, e1_datasets, baseline_metadata,
            raw_dir / "e1_postgresql.jsonl", plan_dir,
        )
        run_e2(
            args, connection, e2_datasets, baseline_metadata, natural,
            raw_dir / "e2_postgresql.jsonl", plan_dir,
        )
        run_scalar_cost(
            args, connection, e2_datasets, baseline_metadata,
            raw_dir / "postgresql_scalar_cost.jsonl", plan_dir,
            metadata_dir / "cost_experiment_preregistration.json",
        )
    finally:
        connection.close()
    print(f"PostgreSQL campaign {args.run_id} complete", flush=True)


if __name__ == "__main__":
    main()
