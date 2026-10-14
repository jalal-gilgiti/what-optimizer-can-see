#!/usr/bin/env python3
"""Execute the preregistered PostgreSQL 14.24 native-E3 mapping sweep."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import os
import platform
import random
import shutil
import statistics
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import psycopg2


ARTIFACT_ROOT = Path(__file__).resolve().parents[3]
STUDY_SOURCE = Path(__file__).resolve().parents[1]
HERE = Path(os.environ.get("STUDY_OUTPUT_DIR", STUDY_SOURCE)).resolve()
PROTOCOL = STUDY_SOURCE / "protocol/experiment_a_protocol.json"
SOURCE = STUDY_SOURCE / "code/task36_mapping_support.c"
DATASET = ARTIFACT_ROOT / "workloads/synthetic/generated/e3-20260830T051639Z/e3_low_high_r10000_seed42.csv"

RAW = HERE / "raw"
NORMALIZED = HERE / "normalized"
PLANS = HERE / "plans"
TRACES = HERE / "traces"
LOGS = HERE / "logs"
FIGURES = HERE / "figures"
TABLES = HERE / "tables"
HASHES = HERE / "hashes"
EXTENSION = RAW / "extension/task36_mapping_support.so"

PG_BIN = Path("/usr/lib/postgresql/14/bin")
PG_SHARE = Path("/usr/share/postgresql/14")
PG_PKGLIB = Path("/usr/lib/postgresql/14/lib")
PG_CONFIG = PG_BIN / "pg_config"
PORT = 55448
SCALES = [25, 50, 100, 200, 400, 800, 1600, 3200, 6400]
CELLS = [
    ("LOW", "M1"),
    ("LOW", "M2"),
    ("HIGH", "M1"),
    ("HIGH", "M2"),
]
WORK_UNITS = {
    ("LOW", "M1"): 505,
    ("HIGH", "M1"): 505,
    ("LOW", "M2"): 10,
    ("HIGH", "M2"): 1000,
}
MODE = {"M1": "M1_GLOBAL", "M2": "M2_CONDITIONED"}
QUERIES = {
    "LOW": "SELECT COUNT(*)::bigint, SUM(e3_dynamic_loop(value, loop_count))::numeric FROM e3_data WHERE population='LOW' AND e3_dynamic_loop(value, loop_count) >= 0",
    "HIGH": "SELECT COUNT(*)::bigint, SUM(e3_dynamic_loop(value, loop_count))::numeric FROM e3_data WHERE population='HIGH' AND e3_dynamic_loop(value, loop_count) >= 0",
}
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def output_hash(rows: list[tuple[Any, ...]]) -> str:
    normalized = sorted(
        [[str(value) if value is not None else None for value in row] for row in rows],
        key=canonical_json,
    )
    return hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")


def write_json(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, indent=2, sort_keys=True, default=str))


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run_text(args: list[str]) -> str:
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout.strip()


def walk_plan(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield node
    for child in node.get("Plans", []):
        yield from walk_plan(child)


def scan_node(plan: dict[str, Any]) -> dict[str, Any]:
    for node in walk_plan(plan["Plan"]):
        if node.get("Relation Name") == "e3_data":
            return node
    raise RuntimeError("EXPLAIN contained no e3_data scan")


def index_name(plan: dict[str, Any]) -> str:
    for node in walk_plan(plan["Plan"]):
        if node.get("Index Name"):
            return str(node["Index Name"])
    return ""


def signature(plan: dict[str, Any]) -> tuple[str, str]:
    scan = scan_node(plan)
    value = {
        "node_types": [node.get("Node Type") for node in walk_plan(plan["Plan"])],
        "scan_type": scan.get("Node Type"),
        "index_name": index_name(plan),
        "index_cond": scan.get("Index Cond"),
        "filter": scan.get("Filter"),
    }
    payload = canonical_json(value)
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_label(plan: dict[str, Any]) -> str:
    scan = scan_node(plan)
    idx = index_name(plan)
    return f"{scan['Node Type']}:{idx}" if idx else str(scan["Node Type"])


def quantile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def bootstrap_median_ci(values: list[float], seed: int, resamples: int = 10_000) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(values)
    medians = [statistics.median(values[rng.randrange(n)] for _ in range(n)) for _ in range(resamples)]
    return quantile(medians, 0.025), quantile(medians, 0.975)


def summarize(values: list[float], seed: int) -> dict[str, float]:
    mean = statistics.mean(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    q1, q3 = quantile(values, 0.25), quantile(values, 0.75)
    ci_low, ci_high = bootstrap_median_ci(values, seed)
    return {
        "median": statistics.median(values),
        "iqr": q3 - q1,
        "cv": sd / mean if mean else 0.0,
        "ci_low": ci_low,
        "ci_high": ci_high,
    }


def candidate_costs(metrics: dict[str, Any]) -> tuple[float | None, float | None, float | None, float | None]:
    seq = float(metrics["seq"]["total"])
    idx = float(metrics["index"]["total"])
    bmp = float(metrics["bitmap"]["total"])
    seq_value = seq if seq >= 0 else None
    idx_value = idx if idx >= 0 else None
    bmp_value = bmp if bmp >= 0 else None
    available = sorted(v for v in (seq_value, idx_value, bmp_value) if v is not None)
    gap = available[0] - available[1] if len(available) >= 2 else None
    return idx_value, bmp_value, seq_value, gap


def connect(socket: Path, database: str = "task15b_e3"):
    connection = psycopg2.connect(
        host=str(socket),
        port=PORT,
        dbname=database,
        user="experiment",
        connect_timeout=5,
        application_name="e3-mapping-sensitivity-final",
    )
    connection.autocommit = True
    return connection


def set_session(conn, population: str, representation: str, scale: int) -> None:
    del population
    with conn.cursor() as cursor:
        cursor.execute("SET statement_timeout='120000ms'")
        cursor.execute("SET jit=off")
        cursor.execute("SET max_parallel_workers_per_gather=0")
        cursor.execute("SET enable_seqscan=on")
        cursor.execute("SET enable_indexscan=on")
        cursor.execute("SET enable_bitmapscan=on")
        cursor.execute("SET task15b.mode=%s", (MODE[representation],))
        cursor.execute("SET task15b.work_units_per_cost_unit=%s", (scale,))


def capture_plan(
    conn,
    population: str,
    representation: str,
    scale: int,
    analyze: bool,
) -> tuple[dict[str, Any], dict[str, Any], float]:
    set_session(conn, population, representation, scale)
    options = (
        "ANALYZE TRUE, TIMING FALSE, SUMMARY TRUE, VERBOSE TRUE, COSTS TRUE, FORMAT JSON"
        if analyze
        else "VERBOSE TRUE, COSTS TRUE, FORMAT JSON"
    )
    with conn.cursor() as cursor:
        cursor.execute("SELECT task15b_reset_metrics()")
        started = time.perf_counter_ns()
        cursor.execute(f"EXPLAIN ({options}) {QUERIES[population]}")
        plan = cursor.fetchone()[0][0]
        wall_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        cursor.execute("SELECT task15b_metrics()")
        metrics = json.loads(cursor.fetchone()[0])
    return plan, metrics, wall_ms


def execute_semantics(conn, population: str, representation: str, scale: int) -> tuple[list[tuple[Any, ...]], float]:
    set_session(conn, population, representation, scale)
    with conn.cursor() as cursor:
        started = time.perf_counter_ns()
        cursor.execute(QUERIES[population])
        rows = cursor.fetchall()
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
    return rows, elapsed_ms


def setup_database(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute("CREATE EXTENSION plpython3u")
        cursor.execute("LOAD %s", (str(EXTENSION),))
        cursor.execute(
            f"CREATE FUNCTION task15b_cost_support(internal) RETURNS internal "
            f"AS '{EXTENSION}', 'task15b_cost_support' LANGUAGE C STRICT"
        )
        cursor.execute(
            f"CREATE FUNCTION task15b_reset_metrics() RETURNS void "
            f"AS '{EXTENSION}', 'task15b_reset_metrics' LANGUAGE C STRICT"
        )
        cursor.execute(
            f"CREATE FUNCTION task15b_metrics() RETURNS text "
            f"AS '{EXTENSION}', 'task15b_metrics' LANGUAGE C STRICT"
        )
        cursor.execute(
            f"""
            CREATE FUNCTION e3_dynamic_loop(value bigint, iterations bigint)
            RETURNS bigint LANGUAGE plpython3u IMMUTABLE STRICT PARALLEL UNSAFE
            SUPPORT task15b_cost_support COST 100
            AS $PYTHON$
{PLPYTHON_BODY}
$PYTHON$
            """
        )
        cursor.execute("CREATE TABLE e3_data (id bigint, value bigint, population text, loop_count bigint)")
        with DATASET.open("r", encoding="utf-8", newline="") as handle:
            cursor.copy_expert(
                "COPY e3_data (id,value,population,loop_count) FROM STDIN WITH (FORMAT CSV, HEADER TRUE)",
                handle,
            )
        cursor.execute(
            "CREATE INDEX e3_population_expression_idx "
            "ON e3_data (population, e3_dynamic_loop(value, loop_count))"
        )
        cursor.execute("ANALYZE e3_data")


def database_snapshot(conn) -> dict[str, Any]:
    result: dict[str, Any] = {}
    with conn.cursor() as cursor:
        cursor.execute("SELECT version()")
        result["version"] = cursor.fetchone()[0]
        cursor.execute("SHOW cpu_operator_cost")
        result["cpu_operator_cost"] = cursor.fetchone()[0]
        cursor.execute(
            "SELECT name,setting FROM pg_settings WHERE name IN "
            "('jit','max_parallel_workers_per_gather','enable_seqscan','enable_indexscan','enable_bitmapscan') "
            "ORDER BY name"
        )
        result["gucs"] = dict(cursor.fetchall())
        cursor.execute("SELECT indexname,indexdef FROM pg_indexes WHERE tablename='e3_data' ORDER BY indexname")
        result["indexes"] = cursor.fetchall()
        cursor.execute(
            "SELECT attname,null_frac,n_distinct,most_common_vals::text,most_common_freqs::text "
            "FROM pg_stats WHERE tablename='e3_data' ORDER BY attname"
        )
        result["statistics"] = cursor.fetchall()
        cursor.execute(
            "SELECT count(*),count(*) FILTER (WHERE population='LOW'),"
            "count(*) FILTER (WHERE population='HIGH'),min(loop_count),max(loop_count) FROM e3_data"
        )
        result["data_summary"] = cursor.fetchone()
        cursor.execute(
            "SELECT p.procost::float8,p.prosupport::regproc::text,p.prosrc,l.lanname,"
            "p.provolatile,p.proparallel,p.proisstrict FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang "
            "WHERE p.oid='e3_dynamic_loop(bigint,bigint)'::regprocedure"
        )
        result["udf"] = cursor.fetchone()
    return result


def memory_total() -> str:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemTotal:"):
            return line.split(":", 1)[1].strip()
    return "UNKNOWN"


def cpu_model() -> str:
    for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor() or "UNKNOWN"


def trace_row(
    grid: str,
    scale: int,
    population: str,
    representation: str,
    plan: dict[str, Any],
    metrics: dict[str, Any],
    wall_ms: float,
) -> dict[str, Any]:
    scan = scan_node(plan)
    sig_json, sig_hash = signature(plan)
    idx_cost, bmp_cost, seq_cost, gap = candidate_costs(metrics)
    return {
        "grid": grid,
        "K": scale,
        "query": population,
        "representation": representation,
        "procedural_work": WORK_UNITS[(population, representation)],
        "pg_per_tuple_cost": WORK_UNITS[(population, representation)] / scale,
        "selected_plan": plan_label(plan),
        "scan_type": scan["Node Type"],
        "index_name": index_name(plan),
        "plan_signature": sig_hash,
        "plan_signature_json": sig_json,
        "startup_cost": scan.get("Startup Cost"),
        "total_cost": scan.get("Total Cost"),
        "estimated_rows": scan.get("Plan Rows"),
        "planning_wall_ms": wall_ms,
        "support_calls": metrics["support_calls"],
        "support_elapsed_ns": metrics["support_elapsed_ns"],
        "index_scan_path_cost": idx_cost,
        "bitmap_path_cost": bmp_cost,
        "seq_scan_path_cost": seq_cost,
        "best_minus_second_best_cost": gap,
        "set_cheapest_outcome": plan_label(plan),
    }


def deterministic_plan_archive(output: Path) -> None:
    with output.open("xb") as raw_handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_handle, mtime=0) as gzip_handle:
            with tarfile.open(fileobj=gzip_handle, mode="w") as archive:
                for path in sorted(p for p in PLANS.rglob("*") if p.is_file()):
                    info = archive.gettarinfo(str(path), arcname=str(path.relative_to(HERE)))
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = 0
                    with path.open("rb") as source_handle:
                        archive.addfile(info, source_handle)


def contiguous_runs(values: list[int]) -> list[list[int]]:
    if not values:
        return []
    positions = {value: index for index, value in enumerate(SCALES)}
    ordered = sorted(values, key=lambda value: positions[value])
    runs = [[ordered[0]]]
    for value in ordered[1:]:
        if positions[value] == positions[runs[-1][-1]] + 1:
            runs[-1].append(value)
        else:
            runs.append([value])
    return runs


def figure_style() -> None:
    """Times-family, 12 pt, all-black text for publication figures."""
    plt.rcParams.update({
        "font.family": "serif",
        # STIXGeneral is a Times-metric TTF bundled with matplotlib; it is
        # preferred over the Nimbus Roman OTF, which fonttype-42 embeds badly.
        "font.serif": ["Times New Roman", "STIXGeneral", "Nimbus Roman",
                       "Times", "DejaVu Serif"],
        "mathtext.fontset": "stix",
        "font.size": 12,
        "axes.labelsize": 12,
        "axes.titlesize": 12,
        "legend.fontsize": 12,
        "xtick.labelsize": 12,
        "ytick.labelsize": 12,
        "text.color": "black",
        "axes.labelcolor": "black",
        "axes.edgecolor": "black",
        "xtick.color": "black",
        "ytick.color": "black",
        "pdf.fonttype": 42,
    })


def make_figures(primary: list[dict[str, Any]]) -> None:
    figure_style()
    make_sensitivity_figure(primary)
    make_margin_figure(primary)


def make_sensitivity_figure(primary: list[dict[str, Any]]) -> None:
    colors = {("LOW", "M1"): "#0072B2", ("LOW", "M2"): "#D55E00", ("HIGH", "M1"): "#009E73", ("HIGH", "M2"): "#CC79A7"}
    markers = {("LOW", "M1"): "o", ("LOW", "M2"): "s", ("HIGH", "M1"): "^", ("HIGH", "M2"): "D"}
    categories = {"Seq Scan": 0, "Bitmap Heap Scan": 1, "Index Scan": 2, "Index Only Scan": 2, "other": 3}
    offsets = {("LOW", "M1"): -0.12, ("LOW", "M2"): -0.04, ("HIGH", "M1"): 0.04, ("HIGH", "M2"): 0.12}
    fig, ax = plt.subplots(figsize=(6.8, 2.1))
    for cell in CELLS:
        rows = sorted((r for r in primary if (r["query"], r["representation"]) == cell), key=lambda r: r["K"])
        ys = [categories.get(r["scan_type"], 3) + offsets[cell] for r in rows]
        ax.plot([r["K"] for r in rows], ys, marker=markers[cell], color=colors[cell], linewidth=1.6, markersize=5, label=f"{cell[0]} {cell[1]}")
    ax.set_xscale("log", base=2)
    ax.set_xticks(SCALES, [str(value) for value in SCALES])
    ax.set_yticks([0, 1, 2, 3], ["Seq Scan", "Bitmap Heap Scan", "Index Scan", "other"])
    # "other" is an empty category: reuse its band for the legend so the panel
    # can be short without dropping any label.
    ax.set_ylim(-0.32, 3.30)
    ax.set_xlabel("Work units per PostgreSQL cost unit, K", labelpad=2)
    ax.set_ylabel("Selected access path", labelpad=2)
    ax.grid(axis="x", color="#dddddd", linewidth=0.6)
    ax.legend(ncol=4, frameon=False, loc="upper center", bbox_to_anchor=(0.5, 1.02),
              handlelength=1.6, handletextpad=0.5, columnspacing=1.4, borderpad=0.0)
    ax.tick_params(pad=1.5)
    fig.tight_layout(pad=0.25)
    for suffix in ("pdf", "svg", "png"):
        fig.savefig(FIGURES / f"e3_mapping_sensitivity.{suffix}", dpi=300, bbox_inches="tight")
    plt.close(fig)


def make_margin_figure(primary: list[dict[str, Any]]) -> None:
    colors = {("LOW", "M1"): "#0072B2", ("LOW", "M2"): "#D55E00", ("HIGH", "M1"): "#009E73", ("HIGH", "M2"): "#CC79A7"}
    markers = {("LOW", "M1"): "o", ("LOW", "M2"): "s", ("HIGH", "M1"): "^", ("HIGH", "M2"): "D"}
    fig, ax = plt.subplots(figsize=(6.8, 3.4))
    for cell in CELLS:
        rows = sorted((r for r in primary if (r["query"], r["representation"]) == cell and r["best_minus_second_best_cost"] is not None), key=lambda r: r["K"])
        ax.plot([r["K"] for r in rows], [r["best_minus_second_best_cost"] for r in rows], marker=markers[cell], color=colors[cell], linewidth=1.4, markersize=4.5, label=f"{cell[0]} {cell[1]}")
    ax.axhline(0.0, color="black", linewidth=0.8, linestyle="--")
    ax.set_xscale("log", base=2)
    ax.set_xticks(SCALES, [str(value) for value in SCALES])
    ax.set_xlabel("Work units per PostgreSQL cost unit, K")
    ax.set_ylabel("Best minus second-best recorded path cost")
    ax.grid(color="#dddddd", linewidth=0.6)
    ax.legend(ncol=2, frameon=False, loc="best")
    fig.tight_layout()
    for suffix in ("pdf", "svg", "png"):
        fig.savefig(FIGURES / f"e3_mapping_path_cost_margin.{suffix}", dpi=300, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    started_at = utc_now()
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    if protocol["status"] != "FROZEN_BEFORE_NEW_PLAN_OBSERVATION":
        raise RuntimeError("sensitivity protocol is not frozen")
    if protocol["scales_work_units_per_pg_cost_unit"] != SCALES:
        raise RuntimeError("sensitivity grid differs from frozen protocol")
    if sha256(DATASET) != protocol["dataset"]["sha256"]:
        raise RuntimeError("frozen dataset hash mismatch")
    if (NORMALIZED / "e3_mapping_sensitivity_final.csv").exists():
        raise RuntimeError("refusing to overwrite completed sensitivity evidence")

    for directory in (RAW, NORMALIZED, PLANS / "initial", PLANS / "analyze", PLANS / "refinement", TRACES, LOGS, FIGURES, TABLES, HASHES, EXTENSION.parent):
        directory.mkdir(parents=True, exist_ok=True)

    includedir = run_text([str(PG_CONFIG), "--includedir-server"])
    compile_command = [
        "gcc", "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Wno-unused-parameter",
        "-I", includedir, "-o", str(EXTENSION), str(SOURCE),
    ]
    subprocess.run(compile_command, check=True)
    gcc_version = run_text(["gcc", "--version"]).splitlines()[0]
    make_version = run_text(["make", "--version"]).splitlines()[0]
    pg_config_version = run_text([str(PG_CONFIG), "--version"])
    os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    kernel = run_text(["uname", "-a"])

    cluster = Path(f"/tmp/e3-mapping-pg14-{os.getpid()}")
    socket = Path(f"/tmp/e3-mapping-socket-{os.getpid()}")
    if cluster.exists() or socket.exists():
        raise RuntimeError("isolated runtime path already exists")
    socket.mkdir(mode=0o700)
    log_path = LOGS / "postgres.log"
    process = None
    conn = None
    planning_rows: list[dict[str, Any]] = []
    refinement_rows: list[dict[str, Any]] = []
    semantic_rows: list[dict[str, Any]] = []
    raw_trials: list[dict[str, Any]] = []
    snapshot: dict[str, Any] = {}
    selected: dict[tuple[int, str, str], str] = {}
    log_handle = log_path.open("x", encoding="utf-8")
    try:
        subprocess.run(
            [
                str(PG_BIN / "initdb"), "-D", str(cluster), "-L", str(PG_SHARE),
                "--encoding=UTF8", "--locale=C.UTF-8", "--auth-local=trust", "--auth-host=reject",
                "--username=experiment",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        with (cluster / "postgresql.conf").open("a", encoding="utf-8") as handle:
            handle.write("\n# E3 mapping-sensitivity isolated experiment\n")
            handle.write("listen_addresses = ''\n")
            handle.write(f"port = {PORT}\n")
            handle.write(f"unix_socket_directories = '{socket}'\n")
            handle.write("unix_socket_permissions = 0700\n")
            handle.write(f"dynamic_library_path = '{PG_PKGLIB}'\n")
            handle.write("jit = off\nmax_parallel_workers = 0\nmax_parallel_workers_per_gather = 0\n")
            handle.write("log_min_messages = log\nlog_statement = 'none'\n")
        process = subprocess.Popen(
            [str(PG_BIN / "postgres"), "-D", str(cluster)],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(200):
            if process.poll() is not None:
                raise RuntimeError("PostgreSQL exited during startup")
            try:
                admin = connect(socket, "postgres")
                admin.close()
                break
            except psycopg2.OperationalError:
                time.sleep(0.1)
        else:
            raise RuntimeError("PostgreSQL startup timed out")
        admin = connect(socket, "postgres")
        with admin.cursor() as cursor:
            cursor.execute("CREATE DATABASE task15b_e3")
        admin.close()
        conn = connect(socket)
        setup_database(conn)
        snapshot = database_snapshot(conn)
        if not str(snapshot["version"]).startswith("PostgreSQL 14.24 "):
            raise RuntimeError(f"wrong PostgreSQL server: {snapshot['version']}")

        # The complete 36-cell fixed planning grid is observed before refinement.
        for scale in SCALES:
            for population, representation in CELLS:
                plan, metrics, wall_ms = capture_plan(conn, population, representation, scale, analyze=False)
                row = trace_row("INITIAL", scale, population, representation, plan, metrics, wall_ms)
                if row["support_calls"] < 1:
                    raise RuntimeError(f"SupportRequestCost not called for {scale}/{population}/{representation}")
                planning_rows.append(row)
                selected[(scale, population, representation)] = row["selected_plan"]
                write_json(PLANS / "initial" / f"K{scale}_{population}_{representation}.json", plan)

        # Deterministic integer bisection of every adjacent fixed-grid transition.
        for population, representation in CELLS:
            for left, right in zip(SCALES, SCALES[1:]):
                left_label = selected[(left, population, representation)]
                right_label = selected[(right, population, representation)]
                if left_label == right_label:
                    continue
                lo, hi = left, right
                lo_label, hi_label = left_label, right_label
                iterations = 0
                while hi - lo > 1 and iterations < 20:
                    mid = (lo + hi) // 2
                    plan, metrics, wall_ms = capture_plan(conn, population, representation, mid, analyze=False)
                    row = trace_row("REFINEMENT", mid, population, representation, plan, metrics, wall_ms)
                    planning_rows.append(row)
                    write_json(PLANS / "refinement" / f"K{mid}_{population}_{representation}.json", plan)
                    label = row["selected_plan"]
                    if label == lo_label:
                        lo, lo_label = mid, label
                    else:
                        hi, hi_label = mid, label
                    iterations += 1
                refinement_rows.append({
                    "query": population,
                    "representation": representation,
                    "lower_K": lo,
                    "upper_K": hi,
                    "lower_plan": lo_label,
                    "upper_plan": hi_label,
                    "transition_interval_width": hi - lo,
                    "iterations": iterations,
                })

        # Semantic checks retain every one of the 36 outputs and gate interpretation.
        semantic_values: dict[tuple[int, str, str], list[tuple[Any, ...]]] = {}
        for scale in SCALES:
            for population, representation in CELLS:
                rows, elapsed_ms = execute_semantics(conn, population, representation, scale)
                semantic_values[(scale, population, representation)] = rows
                semantic_rows.append({
                    "K": scale,
                    "query": population,
                    "representation": representation,
                    "output_hash": output_hash(rows),
                    "aggregate_count": rows[0][0],
                    "aggregate_checksum": rows[0][1],
                    "statement_ms": elapsed_ms,
                    "semantic_valid": "PENDING",
                })
        for scale in SCALES:
            for population in ("LOW", "HIGH"):
                equal = semantic_values[(scale, population, "M1")] == semantic_values[(scale, population, "M2")]
                for row in semantic_rows:
                    if row["K"] == scale and row["query"] == population:
                        row["semantic_valid"] = equal
                if not equal:
                    raise RuntimeError(f"semantic mismatch at K={scale}, query={population}")

        # Three warmup and ten measured randomized complete blocks over all 36 cells.
        rng = random.Random(3600)
        all_cells = [(scale, population, representation) for scale in SCALES for population, representation in CELLS]
        for phase, blocks in (("warmup", 3), ("measured", 10)):
            for block in range(1, blocks + 1):
                order = list(all_cells)
                rng.shuffle(order)
                for position, (scale, population, representation) in enumerate(order, start=1):
                    plan, metrics, wall_ms = capture_plan(conn, population, representation, scale, analyze=True)
                    scan = scan_node(plan)
                    sig_json, sig_hash = signature(plan)
                    filename = f"{phase}_b{block:02d}_p{position:02d}_K{scale}_{population}_{representation}.json"
                    write_json(PLANS / "analyze" / filename, plan)
                    raw_trials.append({
                        "phase": phase,
                        "block": block,
                        "order_position": position,
                        "K": scale,
                        "query": population,
                        "representation": representation,
                        "procedural_work": WORK_UNITS[(population, representation)],
                        "pg_per_tuple_cost": WORK_UNITS[(population, representation)] / scale,
                        "selected_plan": plan_label(plan),
                        "scan_type": scan["Node Type"],
                        "index_name": index_name(plan),
                        "plan_signature": sig_hash,
                        "plan_signature_json": sig_json,
                        "planning_time_ms": plan.get("Planning Time"),
                        "execution_time_ms": plan.get("Execution Time"),
                        "statement_wall_ms": wall_ms,
                        "support_calls": metrics["support_calls"],
                        "support_elapsed_ns": metrics["support_elapsed_ns"],
                        "actual_rows": scan.get("Actual Rows"),
                    })
    finally:
        if conn is not None:
            conn.close()
        if process is not None:
            subprocess.run(
                [str(PG_BIN / "pg_ctl"), "-D", str(cluster), "-m", "fast", "stop"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
        log_handle.close()
        if cluster.exists():
            shutil.rmtree(cluster)
        if socket.exists():
            shutil.rmtree(socket)

    primary = [row for row in planning_rows if row["grid"] == "INITIAL"]
    planning_lookup = {(row["K"], row["query"], row["representation"]): row for row in primary}
    semantic_lookup = {(row["K"], row["query"], row["representation"]): row for row in semantic_rows}
    normalized_rows: list[dict[str, Any]] = []
    for cell_index, (scale, population, representation) in enumerate(
        (item for scale in SCALES for item in [(scale, *cell) for cell in CELLS])
    ):
        trials = [
            row for row in raw_trials
            if row["phase"] == "measured" and row["K"] == scale
            and row["query"] == population and row["representation"] == representation
        ]
        execution = summarize([float(row["execution_time_ms"]) for row in trials], 3601 + cell_index)
        planning = summarize([float(row["planning_time_ms"]) for row in trials], 5601 + cell_index)
        plan_row = planning_lookup[(scale, population, representation)]
        semantic_row = semantic_lookup[(scale, population, representation)]
        observed_labels = sorted({row["selected_plan"] for row in trials})
        high_variance = execution["cv"] > 0.10
        notes = []
        if observed_labels != [plan_row["selected_plan"]]:
            notes.append("plan changed across measured trials: " + ";".join(observed_labels))
        if high_variance:
            notes.append("HIGH VARIANCE; runtime speedup claims prohibited")
        notes.append("30-trial diagnostic not run; optional under protocol")
        normalized_rows.append({
            "K": scale,
            "query": population,
            "representation": representation,
            "procedural_work": WORK_UNITS[(population, representation)],
            "pg_per_tuple_cost": WORK_UNITS[(population, representation)] / scale,
            "semantic_valid": semantic_row["semantic_valid"],
            "output_hash": semantic_row["output_hash"],
            "scan_type": plan_row["scan_type"],
            "index_name": plan_row["index_name"],
            "plan_signature": plan_row["plan_signature"],
            "startup_cost": plan_row["startup_cost"],
            "total_cost": plan_row["total_cost"],
            "estimated_rows": plan_row["estimated_rows"],
            "planning_time_ms": planning["median"],
            "median_runtime_ms": execution["median"],
            "iqr_runtime_ms": execution["iqr"],
            "cv": execution["cv"],
            "ci_low_ms": execution["ci_low"],
            "ci_high_ms": execution["ci_high"],
            "high_variance": high_variance,
            "notes": "; ".join(notes),
            "index_scan_path_cost": plan_row["index_scan_path_cost"],
            "bitmap_path_cost": plan_row["bitmap_path_cost"],
            "seq_scan_path_cost": plan_row["seq_scan_path_cost"],
            "best_minus_second_best_cost": plan_row["best_minus_second_best_cost"],
        })

    final_columns = [
        "K", "query", "representation", "procedural_work", "pg_per_tuple_cost",
        "semantic_valid", "output_hash", "scan_type", "index_name", "plan_signature",
        "startup_cost", "total_cost", "estimated_rows", "planning_time_ms",
        "median_runtime_ms", "iqr_runtime_ms", "cv", "ci_low_ms", "ci_high_ms",
        "high_variance", "notes", "index_scan_path_cost", "bitmap_path_cost",
        "seq_scan_path_cost", "best_minus_second_best_cost",
    ]
    raw_columns = list(raw_trials[0])
    trace_columns = list(planning_rows[0])
    semantic_columns = list(semantic_rows[0])
    boundary_columns = list(refinement_rows[0]) if refinement_rows else [
        "query", "representation", "lower_K", "upper_K", "lower_plan", "upper_plan",
        "transition_interval_width", "iterations",
    ]
    write_csv(RAW / "e3_mapping_sensitivity_raw.csv", raw_trials, raw_columns)
    write_csv(RAW / "semantic_validation.csv", semantic_rows, semantic_columns)
    write_csv(TRACES / "planner_trace.csv", planning_rows, trace_columns)
    write_csv(TRACES / "boundary_refinement.csv", refinement_rows, boundary_columns)
    write_csv(NORMALIZED / "e3_mapping_sensitivity_final.csv", normalized_rows, final_columns)
    write_csv(HERE / "e3_mapping_sensitivity_final.csv", normalized_rows, final_columns)

    # Compact paper table.
    table_rows: list[dict[str, Any]] = []
    for scale in SCALES:
        table_rows.append({
            "K": scale,
            "LOW M1 plan": planning_lookup[(scale, "LOW", "M1")]["scan_type"],
            "LOW M2 plan": planning_lookup[(scale, "LOW", "M2")]["scan_type"],
            "HIGH M1 plan": planning_lookup[(scale, "HIGH", "M1")]["scan_type"],
            "HIGH M2 plan": planning_lookup[(scale, "HIGH", "M2")]["scan_type"],
        })
    table_columns = ["K", "LOW M1 plan", "LOW M2 plan", "HIGH M1 plan", "HIGH M2 plan"]
    write_csv(TABLES / "e3_mapping_sensitivity_table.csv", table_rows, table_columns)
    latex_lines = [
        "\\begin{tabular}{rllll}",
        "\\toprule",
        "$K$ & LOW M1 & LOW M2 & HIGH M1 & HIGH M2 \\\\",
        "\\midrule",
    ]
    for row in table_rows:
        latex_lines.append(
            f"{row['K']} & {row['LOW M1 plan']} & {row['LOW M2 plan']} & "
            f"{row['HIGH M1 plan']} & {row['HIGH M2 plan']} \\\\"
        )
    latex_lines.extend(["\\bottomrule", "\\end{tabular}"])
    write_text(TABLES / "e3_mapping_sensitivity_table.tex", "\n".join(latex_lines))
    write_csv(HERE / "e3_mapping_sensitivity_table.csv", table_rows, table_columns)
    write_text(HERE / "e3_mapping_sensitivity_table.tex", "\n".join(latex_lines))

    make_figures(primary)

    low_divergent = [
        scale for scale in SCALES
        if selected[(scale, "LOW", "M1")] != selected[(scale, "LOW", "M2")]
    ]
    high_divergent = [
        scale for scale in SCALES
        if selected[(scale, "HIGH", "M1")] != selected[(scale, "HIGH", "M2")]
    ]
    runs = contiguous_runs(low_divergent)
    longest = max(runs, key=len) if runs else []
    span = longest[-1] / longest[0] if len(longest) >= 2 else 1.0
    low_boundaries = [row for row in refinement_rows if row["query"] == "LOW"]
    high_boundaries = [row for row in refinement_rows if row["query"] == "HIGH"]
    historical_reproduced = 400 in low_divergent
    if historical_reproduced and len(longest) >= 3 and span >= 4:
        classification = "ROBUSTLY SUPPORTED"
    elif historical_reproduced:
        classification = "SUPPORTED BUT DECISION-SENSITIVE"
    elif low_divergent or high_divergent:
        classification = "PARTIALLY SUPPORTED"
    else:
        classification = "NOT SUPPORTED"

    if low_divergent:
        divergence_text = (
            "primary-grid K={" + ", ".join(str(value) for value in low_divergent) + "}"
        )
        if low_boundaries:
            detail = "; ".join(
                f"{row['query']} {row['representation']} changes between integer K={row['lower_K']} "
                f"({row['lower_plan']}) and K={row['upper_K']} ({row['upper_plan']})"
                for row in low_boundaries
            )
            divergence_text += "; " + detail
    else:
        divergence_text = "none on the fixed grid"

    # K=400 position uses multiplicative distance to the nearest observed LOW boundary.
    boundary_points = [math.sqrt(row["lower_K"] * row["upper_K"]) for row in low_boundaries]
    nearest_factor = min(max(400 / point, point / 400) for point in boundary_points) if boundary_points else math.inf
    if not historical_reproduced:
        historical_position = "no longer reproduced"
    elif len(low_divergent) == 1:
        historical_position = "isolated"
    elif nearest_factor < 1.5:
        historical_position = "near boundary"
    else:
        historical_position = "inside robust region"

    robust_paragraph = (
        "Across the preregistered mapping sweep, the LOW M1/M2 plan divergence occurred at "
        f"{divergence_text}. The historical $K=400$ configuration is classified as {historical_position}; "
        "the result therefore shows that query-conditioned procedural information reaches PostgreSQL's "
        "native path costing and changes an unforced access-path decision over the observed decision region."
    )
    sensitive_paragraph = (
        "The LOW M1/M2 divergence is reproducible at "
        f"{divergence_text}. We therefore interpret the experiment as evidence that conditioned information "
        "can reach native path selection within a cost-sensitive decision region, not as a mapping-independent effect."
    )
    null_paragraph = (
        "The original M1/M2 decision difference does not persist across the sensitivity sweep. We therefore "
        "retain only any faithfully reproduced frozen-configuration observation and do not claim robust decision "
        "reach across work-to-cost mappings."
    )
    if classification == "ROBUSTLY SUPPORTED":
        selected_letter = "A"
    elif classification == "SUPPORTED BUT DECISION-SENSITIVE":
        selected_letter = "B"
    else:
        selected_letter = "C"
    paper_text = "\n\n".join([
        "% A. Robust-result wording",
        robust_paragraph,
        "% B. Decision-sensitive-result wording",
        sensitive_paragraph,
        "% C. Null/inconclusive-result wording",
        null_paragraph,
        f"% SUPPORTED BY THE DATA: paragraph {selected_letter} ({classification}).",
    ])
    write_text(HERE / "E3_MAPPING_RESULT_FOR_PAPER.tex", paper_text)

    if classification == "ROBUSTLY SUPPORTED":
        limitation = (
            "The work-to-cost mapping is not assumed to be universal. In PostgreSQL 14.24, the fixed sensitivity "
            f"grid located the observed LOW decision region as follows: {divergence_text}. This measured boundary "
            "replaces the previously unresolved status of the historical /400 conversion; conclusions remain "
            "limited to this controlled expression-index stress case and tested mapping range."
        )
    elif classification in ("SUPPORTED BUT DECISION-SENSITIVE", "PARTIALLY SUPPORTED"):
        limitation = (
            "The native E3 plan contrast is mapping-sensitive. The fixed sweep found LOW divergence at "
            f"{divergence_text}; the experiment supports decision reach only within that region and does not establish "
            "a mapping-independent optimizer benefit."
        )
    else:
        limitation = (
            "The work-to-cost mapping remains an unresolved limitation because the frozen LOW plan contrast was not "
            "reproduced across the fixed sensitivity grid."
        )
    write_text(HERE / "E3_MAPPING_LIMITATION_FOR_PAPER.tex", limitation)

    def cell_plan(population: str, representation: str) -> str:
        values = [selected[(scale, population, representation)].split(":", 1)[0] for scale in SCALES]
        changes = [
            f"{SCALES[index - 1]}->{SCALES[index]}: {values[index - 1]}->{values[index]}"
            for index in range(1, len(values)) if values[index] != values[index - 1]
        ]
        return "; ".join(changes) if changes else f"stable {values[0]} on K=25..6400"

    high_variance_count = sum(bool(row["high_variance"]) for row in normalized_rows)
    result_summary = {
        "classification": classification,
        "low_divergent_primary_K": low_divergent,
        "high_divergent_primary_K": high_divergent,
        "boundary_refinement": refinement_rows,
        "historical_K400": historical_position,
        "nearest_low_boundary_factor_from_K400": None if math.isinf(nearest_factor) else nearest_factor,
        "high_variance_cells": high_variance_count,
        "primary_cells": len(primary),
        "timing_trials": len(raw_trials),
        "semantic_cells": len(semantic_rows),
    }
    write_json(HERE / "analysis.json", result_summary)

    environment = {
        "started_at_utc": started_at,
        "finished_at_utc": utc_now(),
        "postgresql": snapshot.get("version"),
        "pg_config": pg_config_version,
        "gcc": gcc_version,
        "make": make_version,
        "compiler_flags": compile_command[1:],
        "os_release": os_release,
        "kernel": kernel,
        "cpu": cpu_model(),
        "logical_cpus_visible": os.cpu_count(),
        "ram_visible": memory_total(),
        "git_commit": "NOT_AVAILABLE_EXPORTED_ARTIFACT",
        "extension_source_sha256": sha256(SOURCE),
        "extension_binary_sha256": sha256(EXTENSION),
        "dataset_sha256": sha256(DATASET),
        "protocol_sha256": sha256(PROTOCOL),
        "database_snapshot": snapshot,
        "container_base": "ubuntu:22.04",
        "container_image_digest": "sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
        "cluster_stopped_and_temporary_data_removed": True,
    }
    write_json(HERE / "environment.json", environment)

    readme = f"""# PostgreSQL native E3 work-to-cost mapping sensitivity

Status: **COMPLETED**. Classification: **{classification}**.

## Result

The complete preregistered 9-by-4 grid ran on PostgreSQL 14.24. LOW M1 and M2 diverged at {divergence_text}. HIGH M1/M2 divergence on the primary grid: {high_divergent or 'none'}. The historical K=400 point is **{historical_position}**.

The primary scientific result is access-path selection. Runtime is secondary: {high_variance_count} of 36 primary timing cells exceeded CV 0.10 and are explicitly retained. The optional 30-trial diagnostic block was not run; no speedup is claimed for high-variance cells.

## Frozen experiment verified before execution

- PostgreSQL: `14.24 (Ubuntu 14.24-0ubuntu0.22.04.1)`
- Original mapping: `SupportRequestCost.per_tuple = work_units * cpu_operator_cost = work_units / 400`, with the default `cpu_operator_cost=0.0025`.
- Work representations: M1=505 for LOW/HIGH; M2 LOW=10; M2 HIGH=1000.
- SQL: `{QUERIES['LOW']}` and `{QUERIES['HIGH']}`.
- Table: `e3_data(id bigint,value bigint,population text,loop_count bigint)`; 10,000 rows, 5,000 per population, loop counts 10 and 1000.
- Expression index: `e3_population_expression_idx` on `(population, e3_dynamic_loop(value, loop_count))`.
- Frozen natural paths at K=400: LOW M1 Index Scan, LOW M2 Bitmap Heap Scan, HIGH M1 Index Scan, HIGH M2 Index Scan.
- Search space/settings: sequential, index, and bitmap scans enabled; JIT off; parallel gather off; no hints and no forced paths.
- Existing hashes are preserved in `protocol/original_result_hashes.sha256`; this experiment does not modify frozen results.

## Exact reproduction

From the survey root:

```bash
studies/e3_mapping_sensitivity/code/run_e3_mapping_sensitivity.sh
```

The wrapper builds the `ubuntu:22.04` container and installs the PostgreSQL 14 server/client/PL-Python/server-development packages, build-essential, Python 3.10, psycopg2, and Matplotlib. It compiles with:

```text
{' '.join(compile_command)}
```

The runner initializes a fresh `C.UTF-8` PostgreSQL cluster under `/tmp`, creates PL/Python, loads the compiled support extension, copies the frozen CSV, creates the expression index, runs `ANALYZE`, and records the live statistics in `environment.json`. It then runs all 36 planning cells, performs boundary refinement, validates all 36 semantic cells, and executes randomized complete timing blocks (3 warmups plus 10 retained trials per cell). The temporary cluster is stopped and removed.

## Decision regions

- LOW M1: {cell_plan('LOW', 'M1')}
- LOW M2: {cell_plan('LOW', 'M2')}
- HIGH M1: {cell_plan('HIGH', 'M1')}
- HIGH M2: {cell_plan('HIGH', 'M2')}
- Refined transitions: {canonical_json(refinement_rows) if refinement_rows else 'none observed in K=25..6400'}

`startup_cost`, `total_cost`, and `estimated_rows` in the final CSV refer to the selected `e3_data` scan node. Candidate path costs come from the read-only planner hook; sequential cost is reconstructed with PostgreSQL's native `cost_seqscan` when pruned, exactly as in the frozen bridge. A missing index/bitmap candidate is retained as an empty CSV field rather than imputed. `best_minus_second_best_cost` is the smaller minus the second-smallest available recorded candidate cost, so zero denotes a tie.

## Outputs

- `raw/e3_mapping_sensitivity_raw.csv`: all warmup and measured timing trials.
- `normalized/e3_mapping_sensitivity_final.csv`: required 36-row machine-readable result.
- `plans/`: all fixed-grid, refinement, and per-trial JSON plans.
- `traces/planner_trace.csv`: support-call and candidate-path instrumentation.
- `traces/boundary_refinement.csv`: exact integer transition brackets.
- `figures/`: categorical plan and path-gap figures in PDF/SVG/PNG.
- `tables/`: publication CSV and LaTeX table.
- `E3_MAPPING_RESULT_FOR_PAPER.tex`: three alternatives and selected paragraph.
- `E3_MAPPING_LIMITATION_FOR_PAPER.tex`: measured limitation update.
- `hashes/SHA256SUMS`: integrity manifest, including source, binary, frozen data, raw/normalized CSV, plan archive, figure, and table.
"""
    write_text(HERE / "README.md", readme)

    summary_md = f"""# Native E3 mapping-sensitivity result

Classification: **{classification}**

- LOW divergent fixed-grid K: {low_divergent or 'none'}
- HIGH divergent fixed-grid K: {high_divergent or 'none'}
- Historical K=400: {historical_position}
- Integer-refined transitions: {canonical_json(refinement_rows) if refinement_rows else 'none'}
- Semantic validity: PASS for all 36 cells
- High-variance timing cells: {high_variance_count}/36; retained; no high-variance speedup claim
"""
    write_text(HERE / "summary.md", summary_md)

    deterministic_plan_archive(HASHES / "plans.tar.gz")
    hash_targets = [
        SOURCE,
        EXTENSION,
        DATASET,
        RAW / "e3_mapping_sensitivity_raw.csv",
        NORMALIZED / "e3_mapping_sensitivity_final.csv",
        HASHES / "plans.tar.gz",
        FIGURES / "e3_mapping_sensitivity.pdf",
        FIGURES / "e3_mapping_sensitivity.svg",
        FIGURES / "e3_mapping_sensitivity.png",
        FIGURES / "e3_mapping_path_cost_margin.pdf",
        TABLES / "e3_mapping_sensitivity_table.csv",
        TABLES / "e3_mapping_sensitivity_table.tex",
        HERE / "E3_MAPPING_RESULT_FOR_PAPER.tex",
        HERE / "E3_MAPPING_LIMITATION_FOR_PAPER.tex",
        HERE / "environment.json",
        HERE / "analysis.json",
        HERE / "README.md",
    ]
    manifest_lines = []
    for path in hash_targets:
        try:
            relative = path.relative_to(ARTIFACT_ROOT)
        except ValueError:
            relative = path
        manifest_lines.append(f"{sha256(path)}  {relative}")
    write_text(HASHES / "SHA256SUMS", "\n".join(manifest_lines))

    print(json.dumps(result_summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
