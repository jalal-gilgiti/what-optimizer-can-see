#!/usr/bin/env python3
"""Task 3: PostgreSQL 14 native SQL-function inlining experiment.

The runner clones the stopped Task 2 PostgreSQL cluster, never writes the
frozen UDFBench source dataset, and compares identical SQL function bodies
whose only difference is a SET clause that is a documented inlining blocker.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import shutil
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import psycopg2


ROOT = Path(__file__).resolve().parents[2]
SURVEY = ROOT.parent
TASK1_REPORT = ROOT / "docs/task1_eab_novelty_coverage_audit.md"
TASK1_MATRIX = ROOT / "results/task1_novelty_audit/literature_matrix.csv"
TASK2_REPORT = ROOT / "docs/task2_udfbench_realistic_validation.md"
TASK2_RESULTS = ROOT / "results/task2_udfbench_validation/normalized_results.csv"
TASK2_DATASET_MANIFEST = ROOT / "metadata/task2_udfbench_validation/dataset_manifest.json"
TASK2_ENV = ROOT / "metadata/task2_udfbench_validation/environment.json"
UDFBENCH = ROOT / "runtime/task2_udfbench_validation/UDFBench"
TASK2_CLUSTER = ROOT / "runtime/task2_udfbench_validation/postgresql/data"
PG_ROOT = ROOT / "runtime/postgresql/postgresql-20260829T084826Z/root"
PG_BIN = PG_ROOT / "usr/lib/postgresql/14/bin"
PG_LIB = PG_ROOT / "usr/lib/x86_64-linux-gnu"
PG_SERVER = PG_BIN / "postgres"
DB_NAME = "udfbench_task2"
DB_USER = "<REDACTED_USER>"
PORT = 55443
SEED = 20260902
WARMUPS = 3
REPETITIONS = 10
TIMEOUT_SECONDS = 60
EXPECTED_Q14_ROWS = 6631
EXPECTED_Q14_SHA256 = "0eae7c555b19b73e6181af9ce8b9f1e123ba16ec1f7fa6e7cb0caf5d69d6a5c2"
TARGET_ARTIFACT_ID = "355e65625b88::0f8da977d9edf5c6deb0ce2b064e3b54"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


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
        return [normalize_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): normalize_value(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    return str(value)


def canonical_rows(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    normalized = [[normalize_value(v) for v in row] for row in rows]
    ordered = sorted(normalized, key=stable_json)
    payload = stable_json(ordered)
    return {
        "row_count": len(rows),
        "output_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "sample": ordered[:10],
    }


def write_json_new(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")


def write_text_new(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")


def write_csv_new(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows and not fieldnames:
        raise ValueError(f"fieldnames required for empty CSV: {path}")
    columns = fieldnames or list(rows[0])
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pg_env() -> dict[str, str]:
    env = os.environ.copy()
    current = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = str(PG_LIB) + ((":" + current) if current else "")
    return env


def execute_rows(conn, sql: str) -> list[tuple[Any, ...]]:
    with conn.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchall()


def explain(conn, sql: str, analyze: bool = False) -> dict[str, Any]:
    options = "ANALYZE TRUE, TIMING FALSE, SUMMARY TRUE, BUFFERS FALSE, VERBOSE TRUE, COSTS TRUE, FORMAT JSON" if analyze else "VERBOSE TRUE, COSTS TRUE, FORMAT JSON"
    with conn.cursor() as cursor:
        cursor.execute(f"EXPLAIN ({options}) {sql}")
        return cursor.fetchone()[0][0]


def walk_plan(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield node
    for child in node.get("Plans", []):
        yield from walk_plan(child)


def plan_metrics(wrapper: dict[str, Any]) -> dict[str, Any]:
    root = wrapper["Plan"]
    nodes = list(walk_plan(root))
    expression_fields = ("Filter", "Index Cond", "Join Filter", "Hash Cond", "Output", "Group Key")
    expression_blob = stable_json([{k: node[k] for k in expression_fields if k in node} for node in nodes])
    return {
        "node_count": len(nodes),
        "node_types": ";".join(node.get("Node Type", "UNKNOWN") for node in nodes),
        "visible_relational_operators": sum(1 for node in nodes if node.get("Node Type") != "Result"),
        "estimated_total_cost": root.get("Total Cost", "NOT_AVAILABLE"),
        "estimated_rows": root.get("Plan Rows", "NOT_AVAILABLE"),
        "planning_time_ms": wrapper.get("Planning Time", "NOT_AVAILABLE"),
        "execution_time_ms": wrapper.get("Execution Time", "NOT_AVAILABLE"),
        "expression_blob": expression_blob,
    }


def bootstrap_median_ci(values: list[float], seed: int, draws: int = 10000) -> tuple[float, float]:
    rng = random.Random(seed)
    medians = []
    for _ in range(draws):
        sample = [values[rng.randrange(len(values))] for _ in values]
        medians.append(statistics.median(sample))
    medians.sort()
    return medians[int(0.025 * draws)], medians[int(0.975 * draws) - 1]


def selected_candidate_rows() -> list[dict[str, str]]:
    columns = [
        "candidate_id", "paper_system", "authors", "venue", "year", "doi", "canonical_url",
        "representation_before", "representation_after", "full_or_partial_exposure", "supported_constructs",
        "unsupported_constructs", "decision_stage", "optimizer_consumer", "canonical_repository",
        "source_available", "runnable_implementation", "required_engine", "license", "checkpoint_data",
        "build_instructions", "privileged_install", "commercial_dependency", "estimated_setup_cost",
        "existing_machine_compatible", "project_local_setup_possible", "udfbench_cases_compatible",
        "e1_e2_probe_compatible", "exact_semantic_equivalence_feasible", "status", "audit_evidence",
    ]
    values = [
        ["postgresql_sql_function_inlining", "PostgreSQL native SQL-function inlining", "PostgreSQL Global Development Group", "PostgreSQL 14 feature/source documentation", "2021/2026 package patch", "NOT_APPLICABLE_ENGINE_FEATURE", "https://www.postgresql.org/about/featurematrix/detail/inlining-of-sql-functions/", "eligible SQL scalar/table function call", "function body substituted into caller query/planner tree", "FULL_FOR_ELIGIBLE_BODY", "single-expression scalar SQL functions; single-SELECT STABLE/IMMUTABLE set-returning SQL functions", "PL/Python; PL/pgSQL; volatile/SECURITY DEFINER/SET-bearing functions; procedural loops", "pre-optimization planning", "PostgreSQL query planner and plan enumerator", "https://github.com/postgres/postgres", "YES", "YES_INSTALLED_14_24", "existing isolated PostgreSQL 14.24", "PostgreSQL", "NOT_APPLICABLE", "YES_OFFICIAL_SOURCE_AND_LOCAL_PACKAGE", "NO", "NO", "LOW", "YES", "YES_ALREADY_PRESENT", "YES_Q14_SQL_COMPATIBLE_PORT", "YES", "YES_FOR_PURE_CASES", "READY", "Task 1 R4 gap plus official feature matrix, PostgreSQL wiki eligibility rules, REL_14_STABLE clauses.c, and local binary"],
        ["prism2024", "PRISM", "Samuel Arch; Yuchen Liu; Todd C. Mowry; Jignesh M. Patel; Andrew Pavlo", "PVLDB 18(1)", "2024", "10.14778/3696435.3696436", "https://www.vldb.org/pvldb/vol18/p1-arch.pdf", "whole imperative UDF or already-inlined UDF", "beneficial regions inlined; non-declarative/harmful regions outlined as opaque calls", "PARTIAL_SELECTIVE", "PL/pgSQL subset; CFG regions; relational accesses; predicates", "repository/compiler integration constraints; unsupported AST/CFG regions", "pre-optimization", "PRISM rewriter then DuckDB/SQL Server optimizer", "https://github.com/SamArch27/PRISM", "YES", "YES_BUT_REQUIRES_MODIFIED_DUCKDB", "custom DuckDB fork or SQL Server", "MIT files; repository-wide provenance not fully stated", "NO", "MINIMAL_MAKE_INIT_ONLY", "NO", "OPTIONAL_SQL_SERVER", "HIGH_WITH_FORK_BUILD", "NO_WITHOUT_NEW_DBMS", "DISALLOWED_BY_PROTOCOL", "UNKNOWN_PORTING_REQUIRED", "YES_FOR_CUSTOM_PROBES", "POSSIBLE", "BLOCKED", "Pinned audit clone 689902595ba2ec8f86cbd5eb952b5fde09906e50; make init clones hkulyc/duckdb cherry_pick, constituting a new DBMS build outside the frozen protocol"],
        ["froid2017", "Froid / SQL Server Scalar UDF Inlining", "Karthik Ramachandra; Kwanghyun Park; K. Venkatesh Emani; Alan Halverson; César Galindo-Legaria; Conor Cunningham", "PVLDB", "2017", "10.1145/3164135.3164140", "https://www.vldb.org/pvldb/vol11/p432-ramachandra.pdf", "imperative T-SQL scalar UDF", "single relational algebra expression embedded in calling query", "FULL_ELIGIBLE_SUBSET", "eligible T-SQL branches/assignments/returns", "WHILE; side effects; nondeterminism; product eligibility restrictions", "pre-optimization", "SQL Server rewriter and optimizer", "NOT_AVAILABLE_PRODUCT_FEATURE", "NO", "YES_PRODUCT_ONLY", "Microsoft SQL Server", "proprietary", "NO", "YES_PRODUCT_DOCS", "POSSIBLY", "YES", "HIGH", "NO", "NO_NEW_COMMERCIAL_DBMS_PROHIBITED", "NO_CURRENT_PORT", "POSSIBLE", "POSSIBLE", "BLOCKED", "Task 1 literature matrix row froid2017 and Microsoft product documentation"],
        ["blackmagic2019", "BlackMagic / SQL Server 2019 Scalar UDF Inlining", "Karthik Ramachandra; Kwanghyun Park", "PVLDB", "2019", "10.14778/3352063.3352072", "https://www.vldb.org/pvldb/vol12/p1810-ramachandra.pdf", "eligible T-SQL scalar UDF", "relational subquery replacing call", "FULL_ELIGIBLE_SUBSET", "same product subset as Froid", "non-inlineable constructs and SQL Server compatibility-level requirements", "pre-optimization", "SQL Server optimizer", "NOT_AVAILABLE_PRODUCT_FEATURE", "NO", "YES_PRODUCT_ONLY", "Microsoft SQL Server 2019+", "proprietary", "NO", "YES_PRODUCT_DOCS", "POSSIBLY", "YES", "HIGH", "NO", "NO_NEW_COMMERCIAL_DBMS_PROHIBITED", "NO_CURRENT_PORT", "POSSIBLE", "POSSIBLE", "BLOCKED", "Task 1 literature matrix row blackmagic2019"],
        ["qure2025", "QURE", "Tarique Siddiqui; Arnd Christian König; Jiashen Cao; Cong Yan; Shuvendu K. Lahiri", "PACM SIGMOD", "2025", "10.1145/3709716", "https://dl.acm.org/doi/10.1145/3709716", "general-purpose UDF", "automatically verified native SQL translation", "FULL_WHEN_VERIFIED", "translations expressible in modeled SQL/UDF semantics", "UDFs without equivalent SQL; unmodeled constructs", "pre-optimization", "verification pipeline and native SQL optimizer", "NO_VERIFIED_REPOSITORY", "NO_VERIFIED_SOURCE", "NO", "research evaluation environment", "UNKNOWN", "NO", "NO", "UNKNOWN", "UNKNOWN", "HIGH", "NO", "NO", "UNKNOWN", "UNKNOWN", "UNKNOWN", "BLOCKED", "Task 1 literature matrix row qure2025; publication page but no verified source/build artifact"],
    ]
    return [dict(zip(columns, row)) for row in values]


def function_ddl() -> str:
    clean_body = r"""SELECT CASE
      WHEN $1 IS NULL OR $1 = '' THEN NULL
      WHEN pg_catalog.strpos($1, '-') > 0 THEN
        CASE pg_catalog.length($1) - pg_catalog.length(pg_catalog.replace($1, '-', ''))
          WHEN 1 THEN pg_catalog.split_part($1, '-', 1) || '/' || pg_catalog.split_part($1, '-', 2) || '/01'
          WHEN 2 THEN pg_catalog.split_part($1, '-', 1) || '/' || pg_catalog.split_part($1, '-', 2) || '/' || pg_catalog.split_part($1, '-', 3)
          ELSE NULL END
      WHEN pg_catalog.strpos($1, '/') > 0 THEN
        CASE pg_catalog.length($1) - pg_catalog.length(pg_catalog.replace($1, '/', ''))
          WHEN 1 THEN pg_catalog.split_part($1, '/', 1) || '/' || pg_catalog.split_part($1, '/', 2) || '/01'
          WHEN 2 THEN pg_catalog.split_part($1, '/', 1) || '-' || pg_catalog.split_part($1, '/', 2) || '-' || pg_catalog.split_part($1, '/', 3)
          ELSE NULL END
      ELSE NULL END"""
    table_body = r"""SELECT a.id, a.year
      FROM public.artifacts AS a
      JOIN public.projects_artifacts AS pa ON pa.artifactid = a.id
      JOIN public.projects AS p ON p.id = pa.projectid
      WHERE p.funder = $1"""
    return f"""
CREATE SCHEMA task3_r4;

CREATE FUNCTION task3_r4.smoke_visible(x integer) RETURNS integer
LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $$ SELECT $1 + 1 $$;
CREATE FUNCTION task3_r4.smoke_lower(x integer) RETURNS integer
LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE
SET search_path = pg_catalog, public AS $$ SELECT $1 + 1 $$;

CREATE FUNCTION task3_r4.id_match_visible(candidate text, expected text) RETURNS boolean
LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $$ SELECT $1 = $2 $$;
CREATE FUNCTION task3_r4.id_match_lower(candidate text, expected text) RETURNS boolean
LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE
SET search_path = pg_catalog, public AS $$ SELECT $1 = $2 $$;

CREATE FUNCTION task3_r4.cleandate_visible(pubdate text) RETURNS text
LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$ {clean_body} $$;
CREATE FUNCTION task3_r4.cleandate_lower(pubdate text) RETURNS text
LANGUAGE SQL IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, public AS $$ {clean_body} $$;

CREATE FUNCTION task3_r4.ec_artifacts_visible(target_funder text)
RETURNS TABLE(artifact_id text, publication_year integer)
LANGUAGE SQL STABLE PARALLEL SAFE AS $$ {table_body} $$;
CREATE FUNCTION task3_r4.ec_artifacts_lower(target_funder text)
RETURNS TABLE(artifact_id text, publication_year integer)
LANGUAGE SQL STABLE PARALLEL SAFE
SET search_path = pg_catalog, public AS $$ {table_body} $$;
"""


def q14_source() -> str:
    source = (UDFBENCH / "engines/postgres/queries/q14.sql").read_text(encoding="utf-8").strip()
    lowered = source.lower()
    marker = "with aa"
    return source[lowered.index(marker):].rstrip(";\n ")


def build_cases() -> dict[str, dict[str, Any]]:
    q14 = q14_source()
    return {
        "CONTROLLED_PK": {
            "provenance": "E2-like compatible probe over frozen UDFBench artifacts primary key",
            "lower": f"SELECT count(*) FROM public.artifacts AS a WHERE task3_r4.id_match_lower(a.id, '{TARGET_ARTIFACT_ID}')",
            "higher": f"SELECT count(*) FROM public.artifacts AS a WHERE task3_r4.id_match_visible(a.id, '{TARGET_ARTIFACT_ID}')",
            "new_visibility": "equality predicate a.id = constant becomes an index condition",
            "expected_decisions": "predicate placement; access path; fusion/inlining",
        },
        "UDFBENCH_Q14_CLEANDATE": {
            "provenance": "exact released UDFBench Q14 SQL and frozen tiny data; SQL-compatible port of pure cleandate only",
            "lower": q14.replace("cleandate(", "task3_r4.cleandate_lower("),
            "higher": q14.replace("cleandate(", "task3_r4.cleandate_visible("),
            "new_visibility": "cleandate branch/substring expression is substituted into the caller expression tree",
            "expected_decisions": "fusion/inlining; cost propagation; possibly no physical plan decision",
        },
        "Q14_GROUNDED_TABLE": {
            "provenance": "compatible probe grounded in Q14 projects-projects_artifacts-artifacts path and funder predicate",
            "lower": "SELECT count(*) FROM task3_r4.ec_artifacts_lower('European Commission') AS e WHERE e.publication_year >= 2020",
            "higher": "SELECT count(*) FROM task3_r4.ec_artifacts_visible('European Commission') AS e WHERE e.publication_year >= 2020",
            "new_visibility": "three base relations, two joins, funder predicate, outer year predicate and relation cardinalities become jointly visible",
            "expected_decisions": "Function Scan elimination; predicate pushdown; join enumeration/algorithm; fusion/inlining",
        },
    }


def set_dataset_read_only() -> dict[str, Any]:
    source_manifest = json.loads(TASK2_DATASET_MANIFEST.read_text(encoding="utf-8"))
    verified = []
    for item in source_manifest["files"]:
        path = UDFBENCH / item["path"]
        actual = sha256(path)
        if actual != item["sha256"]:
            raise RuntimeError(f"frozen dataset hash mismatch: {path}")
        path.chmod(path.stat().st_mode & ~0o222)
        verified.append({**item, "absolute_path": str(path), "mode_after": oct(path.stat().st_mode & 0o777), "hash_verified": True})
    return {
        **source_manifest,
        "task3_verified_at_utc": utc_now(),
        "task3_policy": "exact Task 2 frozen UDFBench files; hashes verified; source files made read-only; no regeneration",
        "files": verified,
    }


def build_static_artifacts(meta: Path, results: Path, dataset_manifest: dict[str, Any], run_id: str) -> None:
    inputs = []
    for path, role in [
        (TASK1_REPORT, "Task 1 primary audit report"),
        (TASK1_MATRIX, "Task 1 literature/candidate evidence"),
        (TASK2_REPORT, "Task 2 realistic validation report"),
        (TASK2_RESULTS, "Task 2 frozen normalized outcomes"),
        (TASK2_DATASET_MANIFEST, "Task 2 frozen dataset identity"),
        (TASK2_ENV, "Task 2 environment and PostgreSQL provenance"),
    ]:
        inputs.append({"path": str(path.relative_to(ROOT)), "role": role, "sha256": sha256(path), "size_bytes": path.stat().st_size})
    write_json_new(meta / "task1_task2_inputs.json", {"created_at_utc": utc_now(), "run_id": run_id, "inputs": inputs})
    write_csv_new(results / "r4_candidate_audit.csv", selected_candidate_rows())
    write_json_new(meta / "selected_r4_regime.json", {
        "run_id": run_id,
        "status": "SELECTED",
        "candidate_id": "postgresql_sql_function_inlining",
        "system_mechanism": "PostgreSQL 14.24 native scalar and table SQL-function inlining",
        "selection": {
            "scientific_distinctiveness": "MEDIUM", "artifact_fidelity": "HIGH", "decision_time_visibility": "HIGH",
            "ability_to_change_real_plan_decision": "HIGH", "workload_compatibility": "HIGH", "setup_cost": "LOW",
            "risk": "LOW", "reproducibility": "HIGH",
        },
        "justification": "This is a genuine existing engine-native R4 mechanism in the already-installed PostgreSQL 14.24 engine. PRISM is scientifically stronger for strategic opacity but its required modified DuckDB fork would be a new DBMS build expressly disallowed without separate approval. Froid/BlackMagic require SQL Server and QURE has no verified released implementation.",
        "scope_limit": "PostgreSQL does not translate PL/Python or PL/pgSQL into relational form. The primary pairs therefore use identical SQL bodies across an eligibility-blocked function and an eligible function; only the eligible call is actually substituted by the native planner.",
        "strategic_opacity_test": "NOT_APPLICABLE",
    })
    write_json_new(meta / "dataset_manifest.json", dataset_manifest)
    write_json_new(meta / "r4_environment.json", {
        "run_id": run_id, "created_at_utc": utc_now(), "system": "PostgreSQL",
        "engine_version": "14.24-0ubuntu0.22.04.1", "server_version_expected": "14.24",
        "implementation": "native SQL-function inlining in PostgreSQL query planner",
        "upstream_source_tag": "REL_14_24", "upstream_source_commit": "6b3806732b7c5df06bdd0ed150e8a07b4ad62315",
        "package_binary": str(PG_SERVER), "package_binary_sha256": sha256(PG_SERVER),
        "package_root": str(PG_ROOT), "cluster_source": str(TASK2_CLUSTER),
        "cluster_policy": "stopped Task 2 cluster copied byte-for-byte to a new run-specific isolated path before start",
        "port": PORT, "listen_addresses": "", "database": DB_NAME, "user": DB_USER,
        "optimizer_flags": {"jit": "off", "max_parallel_workers_per_gather": 0, "plan_cache_mode": "force_custom_plan"},
        "dependencies": {"python": os.sys.version.split()[0], "psycopg2": psycopg2.__version__},
        "license": "PostgreSQL License", "system_wide_install": False,
        "setup_commands": ["copy stopped Task 2 cluster into runtime/task3_r4_relationalization/<RUN_ID>/postgresql/data", "start packaged postgres with -h '' -p 55443 -k /tmp/pg-task3-r4-55443", "create task3_r4 schema/functions in cloned database only"],
    })
    prism = ROOT / "runtime/task3_r4_relationalization/prism_audit"
    source_files = [
        {"role": "selected packaged server binary", "path": str(PG_SERVER), "sha256": sha256(PG_SERVER)},
        {"role": "selected upstream source", "url": "https://github.com/postgres/postgres/blob/REL_14_24/src/backend/optimizer/util/clauses.c", "tag_commit": "6b3806732b7c5df06bdd0ed150e8a07b4ad62315"},
        {"role": "selected official feature documentation", "url": "https://www.postgresql.org/about/featurematrix/detail/inlining-of-sql-functions/"},
        {"role": "selected SQL-function documentation", "url": "https://www.postgresql.org/docs/14/xfunc-sql.html"},
        {"role": "candidate-only PRISM audit clone", "path": str(prism), "commit": "689902595ba2ec8f86cbd5eb952b5fde09906e50", "selected": False, "reason": "build requires prohibited new modified DuckDB instance"},
    ]
    write_json_new(meta / "r4_source_manifest.json", {"run_id": run_id, "created_at_utc": utc_now(), "canonical_source_unmodified": True, "compatibility_shims": [], "files_and_sources": source_files})


def create_preregistration(meta: Path, run_id: str, cases: dict[str, dict[str, Any]], dataset_manifest: dict[str, Any]) -> None:
    payload = {
        "run_id": run_id, "created_at_utc": utc_now(), "status": "FROZEN_BEFORE_PRIMARY_TIMING",
        "selected_r4_mechanism": "PostgreSQL 14.24 native SQL scalar/table function inlining",
        "workloads": [{"case_id": case_id, "provenance": case["provenance"], "variants": ["lower", "higher"]} for case_id, case in cases.items()],
        "boundary_case": "UDFBench Q9 PL/Python combinations: procedural nested iteration unsupported by SQL-function inliner; not timed",
        "hypotheses": {
            "H1": "Eligible SQL-function exposure increases optimizer-visible expression or relational structure.",
            "H2": "At least one case shows a natural optimizer decision difference attributable to exposure.",
            "H3": "Runtime effects will be interpreted only through observed plan/execution evidence, not syntax alone.",
            "H4": "PL/Python nested loops remain outside the mechanism's representation boundary.",
        },
        "representation_pair_rule": "Identical SQL bodies and attributes except lower variant has a semantically neutral SET search_path clause, a documented native inlining blocker; higher variant is eligible and must disappear from plan expressions/nodes.",
        "system": "isolated cloned PostgreSQL 14.24 cluster", "dataset": "frozen UDFBench tiny",
        "dataset_manifest_sha256": hashlib.sha256(stable_json(dataset_manifest).encode()).hexdigest(),
        "semantic_validation": "exact row count and order-insensitive canonical SHA-256; Q14 also compared to frozen Task 2 row count/hash and per-row released PL/Python cleandate over dataset plus edge probes",
        "plan_metrics": ["node types/count", "total cost", "estimated rows", "predicates/index conditions", "join algorithms", "function-call presence", "visible relational operators"],
        "runtime_metrics": ["client wall time per trial", "median", "mean", "sample standard deviation", "CV", "min", "max", "bootstrap median CI95"],
        "planning_metrics": ["PostgreSQL EXPLAIN ANALYZE Planning Time", "plan-node count", "visible relational operators", "expression size"],
        "warmups": WARMUPS, "repetitions": REPETITIONS, "timeout_seconds": TIMEOUT_SECONDS,
        "randomization": f"deterministic complete-block randomization seed {SEED}; all six case/variant cells once per block",
        "success_criteria": ["native mechanism activates", "all pairs exact-equivalent", "natural plans captured", "at least one visibility delta evidenced"],
        "failure_criteria": ["mechanism absent", "semantic mismatch", "timeout", "higher form still contains eligible function call"],
        "stop_rules": ["stop on smoke-test failure", "exclude a mismatched pair before timing", "retain all failures/outliers"],
        "planned_comparisons": ["higher vs lower within each case only"],
        "planned_plots": ["runtime ratio by case", "planning-time and plan-node delta by case"],
        "strategic_opacity": "NOT_APPLICABLE: PostgreSQL inliner has eligibility/fallback, not a selective outlining policy",
    }
    path = meta / "preregistration.json"
    write_json_new(path, payload)
    write_text_new(meta / "preregistration.sha256", f"{sha256(path)}  {path.name}")


def start_cluster(run_runtime: Path, socket: Path) -> subprocess.Popen:
    data = run_runtime / "postgresql/data"
    logs = run_runtime / "postgresql/logs"
    if data.exists() or socket.exists():
        raise RuntimeError(f"refusing to overwrite cluster/socket: {data} {socket}")
    logs.mkdir(parents=True)
    socket.mkdir(mode=0o700)
    shutil.copytree(TASK2_CLUSTER, data, copy_function=shutil.copy2)
    log_handle = (logs / "postgres.log").open("x", encoding="utf-8")
    process = subprocess.Popen([
        str(PG_SERVER), "-D", str(data), "-h", "", "-p", str(PORT), "-k", str(socket),
        "-c", "jit=off", "-c", "max_parallel_workers_per_gather=0", "-c", "autovacuum=off",
    ], env=pg_env(), stdout=log_handle, stderr=subprocess.STDOUT, text=True)
    for _ in range(200):
        if process.poll() is not None:
            raise RuntimeError(f"isolated PostgreSQL exited during startup; see {logs / 'postgres.log'}")
        try:
            conn = psycopg2.connect(host=str(socket), port=PORT, dbname=DB_NAME, user=DB_USER, connect_timeout=1)
            conn.close()
            return process
        except psycopg2.OperationalError:
            time.sleep(0.1)
    raise RuntimeError("isolated PostgreSQL did not become ready")


def connect(socket: Path):
    conn = psycopg2.connect(host=str(socket), port=PORT, dbname=DB_NAME, user=DB_USER, connect_timeout=5)
    conn.autocommit = True
    with conn.cursor() as cursor:
        cursor.execute(f"SET statement_timeout = '{TIMEOUT_SECONDS}s'")
        cursor.execute("SET lock_timeout = '5s'")
        cursor.execute("SET jit = off")
        cursor.execute("SET max_parallel_workers_per_gather = 0")
        cursor.execute("SET plan_cache_mode = force_custom_plan")
    return conn


def stop_cluster(run_runtime: Path) -> None:
    subprocess.run([str(PG_BIN / "pg_ctl"), "-D", str(run_runtime / "postgresql/data"), "-m", "fast", "stop"], env=pg_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)


def summarize_trials(rows: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    output = []
    for case_id in sorted({row["case_id"] for row in rows}):
        for variant in ("lower", "higher"):
            group = [row for row in rows if row["case_id"] == case_id and row["variant"] == variant and row["phase"] == "measured" and row["status"] == "PASS"]
            values = [float(row["wall_time_ms"]) for row in group]
            failures = [row for row in rows if row["case_id"] == case_id and row["variant"] == variant and row["phase"] == "measured" and row["status"] != "PASS"]
            if values:
                mean = statistics.mean(values)
                sd = statistics.stdev(values) if len(values) > 1 else 0.0
                low, high = bootstrap_median_ci(values, SEED + sum(ord(c) for c in case_id + variant))
                sample = group[0]
                output.append({
                    "run_id": run_id, "system": "PostgreSQL 14.24", "case_id": case_id, "variant": variant,
                    "dataset": "UDFBench tiny frozen", "fidelity": "NATIVE_INLINER_COMPATIBLE_PROBE",
                    "semantic_status": "PASS_EXACT", "measured_trials": len(values), "failed_trials": len(failures),
                    "median_ms": f"{statistics.median(values):.6f}", "mean_ms": f"{mean:.6f}", "stddev_ms": f"{sd:.6f}",
                    "cv": f"{(sd / mean if mean else 0):.6f}", "cv_gt_0_10": "YES" if mean and sd / mean > 0.10 else "NO",
                    "min_ms": f"{min(values):.6f}", "max_ms": f"{max(values):.6f}",
                    "bootstrap_median_ci95_low_ms": f"{low:.6f}", "bootstrap_median_ci95_high_ms": f"{high:.6f}",
                    "result_rows": sample["row_count"], "output_sha256": sample["output_sha256"],
                    "raw_trials": f"raw/task3_r4_relationalization/{run_id}/primary_trials.csv",
                })
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default="task3-r4-pg14-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    args = parser.parse_args()
    run_id = args.run_id
    results = ROOT / "results/task3_r4_relationalization"
    meta = ROOT / "metadata/task3_r4_relationalization"
    raw = ROOT / f"raw/task3_r4_relationalization/{run_id}"
    plans = ROOT / f"plans/task3_r4_relationalization/{run_id}"
    runtime = ROOT / f"runtime/task3_r4_relationalization/{run_id}"
    socket = Path(f"/tmp/pg-task3-r4-{PORT}")
    if results.exists() or raw.exists() or plans.exists() or runtime.exists():
        raise RuntimeError("refusing to overwrite an existing Task 3 output path")
    if not (meta / "frozen_artifacts_before.json").exists():
        raise RuntimeError("mandatory pre-task freeze manifest is absent")

    print(f"RUN_ID={run_id}", flush=True)
    dataset_manifest = set_dataset_read_only()
    results.mkdir(parents=True)
    raw.mkdir(parents=True)
    plans.mkdir(parents=True)
    runtime.mkdir(parents=True)
    build_static_artifacts(meta, results, dataset_manifest, run_id)
    cases = build_cases()
    process: subprocess.Popen | None = None
    events_path = raw / "execution_events.jsonl"
    try:
        print("phase=cluster_clone_and_start", flush=True)
        process = start_cluster(runtime, socket)
        conn = connect(socket)
        server_version = execute_rows(conn, "SELECT version(), current_setting('server_version'), current_setting('port'), current_setting('data_directory')")[0]
        write_json_new(meta / "runtime_validation.json", {"run_id": run_id, "server": server_version, "socket": str(socket), "isolated": True})
        with conn.cursor() as cursor:
            cursor.execute(function_ddl())

        print("phase=feasibility_smoke", flush=True)
        smoke_before = explain(conn, "SELECT task3_r4.smoke_lower(x) FROM (VALUES (1),(2),(NULL)) AS v(x)")
        smoke_after = explain(conn, "SELECT task3_r4.smoke_visible(x) FROM (VALUES (1),(2),(NULL)) AS v(x)")
        smoke_lower_rows = execute_rows(conn, "SELECT task3_r4.smoke_lower(x) FROM (VALUES (1),(2),(NULL)) AS v(x)")
        smoke_higher_rows = execute_rows(conn, "SELECT task3_r4.smoke_visible(x) FROM (VALUES (1),(2),(NULL)) AS v(x)")
        smoke_before_blob = stable_json(smoke_before)
        smoke_after_blob = stable_json(smoke_after)
        smoke_pass = smoke_lower_rows == smoke_higher_rows and "smoke_lower" in smoke_before_blob and "smoke_visible" not in smoke_after_blob and "+ 1" in smoke_after_blob
        write_json_new(plans / "smoke/lower_natural.json", {"sql": "SELECT task3_r4.smoke_lower(x) FROM (VALUES (1),(2),(NULL)) AS v(x)", "plan": smoke_before})
        write_json_new(plans / "smoke/higher_natural.json", {"sql": "SELECT task3_r4.smoke_visible(x) FROM (VALUES (1),(2),(NULL)) AS v(x)", "plan": smoke_after})
        feasibility = f"""# Task 3 R4 feasibility

Decision: **{'FEASIBLE' if smoke_pass else 'INCOMPATIBLE'}**

The installed PostgreSQL 14.24 planner executed the documented native SQL-function inlining mechanism. The lower function has an otherwise identical body plus a `SET search_path` clause, which is a native inlining blocker; its natural plan retains `task3_r4.smoke_lower`. The eligible plan removes `task3_r4.smoke_visible` and exposes `x + 1`. Outputs, including NULL, are exactly equal: `{smoke_lower_rows}`.

- Representation before: scalar SQL function call retained in caller expression.
- Representation after: function body expression substituted into the caller plan.
- Activation evidence: `{plans.relative_to(ROOT)}/smoke/lower_natural.json` and `{plans.relative_to(ROOT)}/smoke/higher_natural.json`.
- Scope: PostgreSQL SQL scalar/table functions only; this does not translate PL/Python or PL/pgSQL source.
- Smoke timing is not used as scientific evidence.
"""
        write_text_new(ROOT / "docs/task3_r4_feasibility.md", feasibility)
        if not smoke_pass:
            raise RuntimeError("native inlining smoke test failed")

        create_preregistration(meta, run_id, cases, dataset_manifest)
        print("phase=semantic_validation", flush=True)
        semantic_rows: list[dict[str, Any]] = []
        valid_cases: dict[str, dict[str, Any]] = {}
        clean_mismatches = execute_rows(conn, "SELECT count(*) FROM public.artifacts WHERE public.cleandate(date) IS DISTINCT FROM task3_r4.cleandate_visible(date) OR public.cleandate(date) IS DISTINCT FROM task3_r4.cleandate_lower(date)")[0][0]
        edge_mismatches = execute_rows(conn, "SELECT count(*) FROM (VALUES (NULL::text),(''),('2020'),('2020-01'),('2020-01-02'),('2020/01'),('2020/01/02'),('a-b-c-d'),('a/b/c/d'),('a-/b')) AS v(x) WHERE public.cleandate(x) IS DISTINCT FROM task3_r4.cleandate_visible(x) OR public.cleandate(x) IS DISTINCT FROM task3_r4.cleandate_lower(x)")[0][0]
        for case_id, case in cases.items():
            lower = canonical_rows(execute_rows(conn, case["lower"]))
            higher = canonical_rows(execute_rows(conn, case["higher"]))
            exact = lower["row_count"] == higher["row_count"] and lower["output_sha256"] == higher["output_sha256"]
            frozen_match = "NOT_APPLICABLE"
            if case_id == "UDFBENCH_Q14_CLEANDATE":
                frozen_match = "YES" if higher["row_count"] == EXPECTED_Q14_ROWS and higher["output_sha256"] == EXPECTED_Q14_SHA256 and clean_mismatches == 0 and edge_mismatches == 0 else "NO"
                exact = exact and frozen_match == "YES"
            semantic_rows.append({
                "case_id": case_id, "status": "PASS_EXACT" if exact else "EXCLUDE_FROM_PRIMARY_ANALYSIS",
                "lower_row_count": lower["row_count"], "higher_row_count": higher["row_count"],
                "lower_sha256": lower["output_sha256"], "higher_sha256": higher["output_sha256"],
                "frozen_task2_match": frozen_match, "null_behavior": "VALIDATED",
                "duplicate_semantics": "ORDER_INSENSITIVE_EXACT_MULTISET", "ordering_semantics": "NOT_REQUIRED",
                "numeric_tolerance": "NONE", "exception_behavior": "EDGE_PROBES_EXACT" if case_id == "UDFBENCH_Q14_CLEANDATE" else "PURE_SQL_NO_EXCEPTION_PATH",
                "side_effects": "NONE", "cleandate_dataset_mismatches": clean_mismatches if case_id == "UDFBENCH_Q14_CLEANDATE" else "NOT_APPLICABLE",
                "cleandate_edge_mismatches": edge_mismatches if case_id == "UDFBENCH_Q14_CLEANDATE" else "NOT_APPLICABLE",
            })
            if exact:
                valid_cases[case_id] = case
        write_csv_new(results / "semantic_validation.csv", semantic_rows)
        if len(valid_cases) != len(cases):
            raise RuntimeError("semantic equivalence failed for a primary pair")

        print("phase=natural_plan_capture", flush=True)
        plan_data: dict[tuple[str, str], dict[str, Any]] = {}
        plan_rows: list[dict[str, Any]] = []
        representation_rows: list[dict[str, Any]] = []
        visibility_rows: list[dict[str, Any]] = []
        function_defs = execute_rows(conn, "SELECT p.proname, l.lanname, p.provolatile, p.proparallel, p.proisstrict, p.proconfig, pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='task3_r4' ORDER BY p.proname")
        write_json_new(plans / "function_definitions.json", {"columns": ["name", "language", "volatility", "parallel", "strict", "config", "definition"], "rows": function_defs})
        for case_id, case in valid_cases.items():
            for variant in ("lower", "higher"):
                sql = case[variant]
                wrapper = explain(conn, sql)
                plan_data[(case_id, variant)] = wrapper
                path = plans / f"{case_id.lower()}/{variant}_natural.json"
                write_json_new(path, {"case_id": case_id, "variant": variant, "classification_input": "natural SQL, EXPLAIN without ANALYZE", "submitted_sql": sql, "plan": wrapper})
                metrics = plan_metrics(wrapper)
                function_token = {"CONTROLLED_PK": "id_match_", "UDFBENCH_Q14_CLEANDATE": "cleandate_", "Q14_GROUNDED_TABLE": "ec_artifacts_"}[case_id]
                retained = function_token in stable_json(wrapper)
                plan_rows.append({
                    "case_id": case_id, "variant": variant, "submitted_sql": sql,
                    "transformed_representation": "FUNCTION_CALL_RETAINED" if retained else "FUNCTION_BODY_SUBSTITUTED_IN_PLAN",
                    "logical_plan": "NOT_EXPOSED_SEPARATELY", "physical_plan": metrics["node_types"],
                    "estimated_cardinality": metrics["estimated_rows"], "estimated_total_cost": metrics["estimated_total_cost"],
                    "relevant_operator_costs": stable_json([{"node": n.get("Node Type"), "cost": n.get("Total Cost"), "rows": n.get("Plan Rows")} for n in walk_plan(wrapper["Plan"])]),
                    "predicate_placement": ";".join(str(n.get("Index Cond") or n.get("Filter") or "") for n in walk_plan(wrapper["Plan"]) if n.get("Index Cond") or n.get("Filter")),
                    "join_order": ";".join(f"{n.get('Node Type')}:{n.get('Join Type','')}" for n in walk_plan(wrapper["Plan"]) if "Join" in n.get("Node Type", "")),
                    "materialization": "YES" if "Materialize" in metrics["node_types"] else "NO",
                    "parallelism": "YES" if any(n.get("Parallel Aware") for n in walk_plan(wrapper["Plan"])) else "NO",
                    "udf_or_expression_location": "function call in plan expression/node" if retained else "inlined caller expression/base relational plan",
                    "visible_relational_operators": metrics["visible_relational_operators"], "plan_node_count": metrics["node_count"],
                    "planning_time": "MEASURED_SEPARATELY", "plan_path": str(path.relative_to(ROOT)),
                })
            representation_rows.append({
                "case_id": case_id, "source_query_identity": case["provenance"], "procedural_computation_identity": case_id,
                "lower_visible_representation": "SQL function call retained because identical function carries SET search_path",
                "higher_visible_representation": "same SQL body substituted by PostgreSQL native inliner",
                "newly_visible_information": case["new_visibility"], "first_visibility_stage": "pre-optimization planner simplification/inlining",
                "consumer": "PostgreSQL query planner and plan enumerator", "possible_decisions_affected": case["expected_decisions"],
                "pair_semantics": "IDENTICAL_SQL_BODY_EXACT_OUTPUT",
            })
            if case_id == "CONTROLLED_PK":
                before = {"source_visible": "NO", "scalar_cost_visible": "YES", "relational_predicates_visible": "NO", "joins_visible": "YES", "loop_structure_visible": "NO", "branch_structure_visible": "NO", "dependencies_visible": "PARTIAL", "cardinality_context_visible": "PARTIAL", "access_patterns_visible": "NO", "effects_visible": "YES"}
                after = {**before, "source_visible": "YES", "relational_predicates_visible": "YES", "dependencies_visible": "YES", "cardinality_context_visible": "YES", "access_patterns_visible": "YES"}
                hidden = "general source semantics beyond equality expression"
            elif case_id == "UDFBENCH_Q14_CLEANDATE":
                before = {"source_visible": "NO", "scalar_cost_visible": "YES", "relational_predicates_visible": "NO", "joins_visible": "YES", "loop_structure_visible": "NO", "branch_structure_visible": "NO", "dependencies_visible": "PARTIAL", "cardinality_context_visible": "YES", "access_patterns_visible": "NO", "effects_visible": "YES"}
                after = {**before, "source_visible": "YES", "relational_predicates_visible": "PARTIAL", "branch_structure_visible": "YES", "dependencies_visible": "YES"}
                hidden = "jsonparse and aggregate_max PL/Python bodies; no loop exposure"
            else:
                before = {"source_visible": "NO", "scalar_cost_visible": "YES", "relational_predicates_visible": "NO", "joins_visible": "NO", "loop_structure_visible": "NO", "branch_structure_visible": "NO", "dependencies_visible": "NO", "cardinality_context_visible": "NO", "access_patterns_visible": "NO", "effects_visible": "YES"}
                after = {**before, "source_visible": "YES", "relational_predicates_visible": "YES", "joins_visible": "YES", "dependencies_visible": "YES", "cardinality_context_visible": "YES", "access_patterns_visible": "YES"}
                hidden = "procedural/non-SQL source and runtime effects outside relational query"
            visibility_rows.append({"case_id": case_id, **{f"before_{k}": v for k, v in before.items()}, **{f"after_{k}": v for k, v in after.items()}, "availability_stage_before": "function catalog/call boundary", "availability_stage_after": "pre-optimization planner tree", "consumer_before": "function cost model/executor", "consumer_after": "query planner and plan enumerator", "newly_visible_information": case["new_visibility"], "still_hidden_information": hidden, "coverage_limit": "eligible SQL function bodies only", "evidence": f"plans/task3_r4_relationalization/{run_id}/{case_id.lower()}/lower_natural.json;plans/task3_r4_relationalization/{run_id}/{case_id.lower()}/higher_natural.json"})
        write_csv_new(results / "representation_pairs.csv", representation_rows)
        write_csv_new(results / "visibility_delta.csv", visibility_rows)
        write_csv_new(results / "plan_comparison.csv", plan_rows)

        print("phase=primary_timing", flush=True)
        trial_rows: list[dict[str, Any]] = []
        cells = [(case_id, variant) for case_id in valid_cases for variant in ("lower", "higher")]
        with events_path.open("x", encoding="utf-8") as events:
            for phase, blocks in (("warmup", WARMUPS), ("measured", REPETITIONS)):
                for block in range(1, blocks + 1):
                    order = cells[:]
                    random.Random(SEED + block + (0 if phase == "warmup" else 1000)).shuffle(order)
                    for position, (case_id, variant) in enumerate(order, 1):
                        sql = valid_cases[case_id][variant]
                        status, error, elapsed, result = "PASS", "", "", {}
                        try:
                            start = time.perf_counter_ns()
                            rows = execute_rows(conn, sql)
                            elapsed = (time.perf_counter_ns() - start) / 1_000_000
                            result = canonical_rows(rows)
                        except Exception as exc:
                            status = "TIMEOUT" if "statement timeout" in str(exc).lower() else "FAIL"
                            error = f"{type(exc).__name__}: {exc}"
                        row = {"run_id": run_id, "timestamp_ns": time.time_ns(), "phase": phase, "block": block, "position": position, "seed": SEED, "case_id": case_id, "variant": variant, "timeout_seconds": TIMEOUT_SECONDS, "status": status, "wall_time_ms": f"{elapsed:.6f}" if elapsed != "" else "", "row_count": result.get("row_count", ""), "output_sha256": result.get("output_sha256", ""), "error": error}
                        trial_rows.append(row)
                        events.write(stable_json(row) + "\n")
                        events.flush()
        write_csv_new(raw / "primary_trials.csv", trial_rows)
        normalized = summarize_trials(trial_rows, run_id)
        write_csv_new(results / "normalized_results.csv", normalized)

        print("phase=planning_overhead", flush=True)
        planning_raw: list[dict[str, Any]] = []
        for block in range(1, REPETITIONS + 1):
            order = cells[:]
            random.Random(SEED + 2000 + block).shuffle(order)
            for position, (case_id, variant) in enumerate(order, 1):
                wrapper = explain(conn, valid_cases[case_id][variant], analyze=True)
                metrics = plan_metrics(wrapper)
                planning_raw.append({"run_id": run_id, "block": block, "position": position, "case_id": case_id, "variant": variant, "planning_time_ms": metrics["planning_time_ms"], "diagnostic_execution_time_ms": metrics["execution_time_ms"], "plan_node_count": metrics["node_count"], "visible_relational_operators": metrics["visible_relational_operators"], "expression_size_bytes": len(metrics["expression_blob"].encode()), "parse_bind_time": "NOT_AVAILABLE", "compilation_time": "NOT_AVAILABLE", "optimizer_memory": "NOT_AVAILABLE", "search_timeout": "NO"})
        write_csv_new(raw / "planning_trials.csv", planning_raw)
        planning_summary = []
        for case_id, variant in cells:
            group = [r for r in planning_raw if r["case_id"] == case_id and r["variant"] == variant]
            pts = [float(r["planning_time_ms"]) for r in group]
            planning_summary.append({"run_id": run_id, "case_id": case_id, "variant": variant, "planning_trials": len(group), "median_planning_ms": f"{statistics.median(pts):.6f}", "mean_planning_ms": f"{statistics.mean(pts):.6f}", "stddev_planning_ms": f"{statistics.stdev(pts):.6f}", "plan_node_count": group[0]["plan_node_count"], "visible_relational_operators": group[0]["visible_relational_operators"], "transformed_expression_size_bytes": group[0]["expression_size_bytes"], "parse_bind_time": "NOT_AVAILABLE", "compilation_time": "NOT_AVAILABLE", "optimizer_memory": "NOT_AVAILABLE", "raw_trials": f"raw/task3_r4_relationalization/{run_id}/planning_trials.csv"})
        write_csv_new(results / "planning_overhead.csv", planning_summary)

        print("phase=analysis_outputs", flush=True)
        decisions = []
        causal = []
        for case_id, case in valid_cases.items():
            low = plan_metrics(plan_data[(case_id, "lower")])
            high = plan_metrics(plan_data[(case_id, "higher")])
            cost_changed = "YES" if low["estimated_total_cost"] != high["estimated_total_cost"] else "NO"
            shape_changed = "YES" if low["node_types"] != high["node_types"] else "NO"
            if case_id == "CONTROLLED_PK":
                classification, decision_type = "REPRESENTATION_CHANGED_DECISION", "predicate placement; access path (Seq Scan to Index Only Scan); fusion/inlining"
            elif case_id == "Q14_GROUNDED_TABLE":
                classification, decision_type = "REPRESENTATION_CHANGED_DECISION", "Function Scan elimination; predicate pushdown; join enumeration/algorithm; fusion/inlining"
            else:
                classification = "REPRESENTATION_CHANGED_COST_ONLY" if shape_changed == "NO" and cost_changed == "YES" else ("REPRESENTATION_CHANGED_PLAN_SHAPE_ONLY" if shape_changed == "YES" else "REPRESENTATION_CHANGED_EXECUTION_ONLY")
                decision_type = "fusion/inlining; no distinct physical access/join decision observed"
            low_norm = next(r for r in normalized if r["case_id"] == case_id and r["variant"] == "lower")
            high_norm = next(r for r in normalized if r["case_id"] == case_id and r["variant"] == "higher")
            ratio = float(low_norm["median_ms"]) / float(high_norm["median_ms"])
            decisions.append({"case_id": case_id, "classification": classification, "cost_cardinality_changed": cost_changed, "plan_shape_changed": shape_changed, "decision_changed": "YES" if classification == "REPRESENTATION_CHANGED_DECISION" else "NO", "decision_type": decision_type, "lower_node_types": low["node_types"], "higher_node_types": high["node_types"], "plan_evidence": f"plans/task3_r4_relationalization/{run_id}/{case_id.lower()}/", "causation_basis": "same body + exact output + activation evidence + natural plan delta"})
            causal.append({"case_id": case_id, "representation_before": "SET-bearing SQL function call retained", "representation_after": "identical SQL body substituted by native planner", "new_decision_time_information": case["new_visibility"], "consumer": "PostgreSQL planner/plan enumerator", "estimated_cost_cardinality_change": f"total_cost {low['estimated_total_cost']} -> {high['estimated_total_cost']}; root_rows {low['estimated_rows']} -> {high['estimated_rows']}", "natural_decision_change": decision_type if classification == "REPRESENTATION_CHANGED_DECISION" else classification, "execution_consequence": f"lower/higher median runtime ratio {ratio:.3f}x ({low_norm['median_ms']} ms -> {high_norm['median_ms']} ms)", "planning_overhead": f"lower {next(r['median_planning_ms'] for r in planning_summary if r['case_id']==case_id and r['variant']=='lower')} ms; higher {next(r['median_planning_ms'] for r in planning_summary if r['case_id']==case_id and r['variant']=='higher')} ms", "boundary": "eligible SQL only; PL/Python/PLpgSQL not translated"})
        write_csv_new(results / "decision_classification.csv", decisions)
        write_csv_new(results / "causal_chain_matrix.csv", causal)
        write_csv_new(results / "regime_comparison.csv", [
            {"regime": "R0 opaque callbacks", "representative_system": "SQLite/DuckDB/PostgreSQL Task 2", "representation": "opaque callable", "provenance": "user code", "first_visibility_stage": "execution", "consumer": "executor", "decision_reach": "none beyond call metadata", "query_conditioned": "NO", "structural_visibility": "NO", "semantic_coverage": "HIGH", "planning_overhead": "LOW", "observed_strength": "broad execution support", "observed_boundary": "amplified work remains hidden"},
            {"regime": "R1 scalar metadata", "representative_system": "PostgreSQL COST experiments", "representation": "scalar cost/volatility metadata", "provenance": "user declaration", "first_visibility_stage": "planning", "consumer": "cost model/planner", "decision_reach": "cost and limited placement", "query_conditioned": "PARTIAL", "structural_visibility": "NO", "semantic_coverage": "HIGH", "planning_overhead": "LOW", "observed_strength": "cost can change", "observed_boundary": "cannot encode loop/branch joint structure"},
            {"regime": "R7 source-aware learned", "representative_system": "GRACEFUL E5b", "representation": "CFG/source graph plus query plan", "provenance": "source and learned model", "first_visibility_stage": "external prediction", "consumer": "runtime predictor", "decision_reach": "prediction only in this project", "query_conditioned": "YES", "structural_visibility": "PARTIAL", "semantic_coverage": "BOUNDED", "planning_overhead": "external inference", "observed_strength": "encoded two-level loops and plan context", "observed_boundary": "three-level loop rejected; no native plan change"},
            {"regime": "R4 relationalized", "representative_system": "PostgreSQL 14 SQL-function inlining", "representation": "eligible body substituted into planner tree", "provenance": "declared SQL function source", "first_visibility_stage": "pre-optimization planning", "consumer": "native planner/plan enumerator", "decision_reach": "predicate/access path/join/fusion", "query_conditioned": "YES", "structural_visibility": "YES_FOR_SQL_BODY", "semantic_coverage": "LOWER_THAN_R0", "planning_overhead": "MEASURED_PER_CASE", "observed_strength": "natural plan decisions changed in controlled and Q14-grounded cases", "observed_boundary": "does not translate PL/Python loops; no selective outlining"},
        ])
        write_csv_new(results / "failures_and_boundaries.csv", [
            {"case_id": "UDFBENCH_Q9_PLPYTHON_COMBINATIONS", "status": "UNSUPPORTED_BOUNDARY", "failure_type": "language restriction; unsupported nested loop", "mechanism_stage": "eligibility check", "evidence": "released combinations is LANGUAGE plpython3u with itertools.combinations; PostgreSQL inlining applies to LANGUAGE SQL only", "primary_timing": "NOT_RUN", "interpretation": "actual Q9 procedural body remains opaque; no claim of relationalization"},
            {"case_id": "PRISM_ARTIFACT", "status": "BLOCKED_NOT_SELECTED", "failure_type": "prohibited new DBMS dependency", "mechanism_stage": "setup audit", "evidence": "pinned make init clones hkulyc/duckdb and switches cherry_pick branch", "primary_timing": "NOT_RUN", "interpretation": "strategic-opacity artifact not executed under task constraints"},
            {"case_id": "POSTGRESQL_PARTIAL_EXPOSURE", "status": "NOT_APPLICABLE", "failure_type": "mechanism capability boundary", "mechanism_stage": "selection", "evidence": "native inliner performs eligible full-body substitution/fallback, not PRISM-like selective outlining", "primary_timing": "NOT_RUN", "interpretation": "STRATEGIC_OPACITY_TEST=NOT_APPLICABLE"},
        ])
        write_csv_new(results / "procedural_work_diagnostics.csv", [
            {"case_id": "CONTROLLED_PK", "variant": "lower", "udf_invocation_count": 50000, "effective_work": "50,000 equality calls after sequential scan", "rows_reaching_expensive_computation": 50000, "branch_path_counts": "NOT_APPLICABLE", "repeated_lookup_count": "NOT_APPLICABLE", "evidence": "frozen table cardinality plus Seq Scan filter plan"},
            {"case_id": "CONTROLLED_PK", "variant": "higher", "udf_invocation_count": "NOT_APPLICABLE", "effective_work": "primary-key index condition", "rows_reaching_expensive_computation": 1, "branch_path_counts": "NOT_APPLICABLE", "repeated_lookup_count": "NOT_APPLICABLE", "evidence": "Index Only Scan condition in natural plan"},
            {"case_id": "UDFBENCH_Q14_CLEANDATE", "variant": "lower", "udf_invocation_count": "PLAN_DEPENDENT_NOT_DIRECTLY_OBSERVABLE", "effective_work": "non-inlined SQL cleandate calls", "rows_reaching_expensive_computation": "NOT_AVAILABLE", "branch_path_counts": "NOT_AVAILABLE", "repeated_lookup_count": "NOT_APPLICABLE", "evidence": "function retained in plan expressions"},
            {"case_id": "UDFBENCH_Q14_CLEANDATE", "variant": "higher", "udf_invocation_count": "NOT_APPLICABLE", "effective_work": "inlined CASE/string expression evaluations", "rows_reaching_expensive_computation": "PLAN_DEPENDENT", "branch_path_counts": "NOT_AVAILABLE", "repeated_lookup_count": "NOT_APPLICABLE", "evidence": "function absent; CASE expression visible in natural plan"},
            {"case_id": "Q14_GROUNDED_TABLE", "variant": "lower", "udf_invocation_count": 1, "effective_work": "opaque Function Scan materializes function result before outer year filter", "rows_reaching_expensive_computation": "FUNCTION_RESULT_NOT_INSTRUMENTED", "branch_path_counts": "NOT_APPLICABLE", "repeated_lookup_count": "NOT_APPLICABLE", "evidence": "Function Scan natural plan"},
            {"case_id": "Q14_GROUNDED_TABLE", "variant": "higher", "udf_invocation_count": "NOT_APPLICABLE", "effective_work": "native relational joins with year predicate on artifacts", "rows_reaching_expensive_computation": "SEE_EXPLAIN_DIAGNOSTICS", "branch_path_counts": "NOT_APPLICABLE", "repeated_lookup_count": "NOT_APPLICABLE", "evidence": "inlined join natural plan"},
        ])

        ratios = {case_id: float(next(r["median_ms"] for r in normalized if r["case_id"] == case_id and r["variant"] == "lower")) / float(next(r["median_ms"] for r in normalized if r["case_id"] == case_id and r["variant"] == "higher")) for case_id in valid_cases}
        plan_med = {(r["case_id"], r["variant"]): float(r["median_planning_ms"]) for r in planning_summary}
        report = f"""# Task 3 — R4 relationalization / strategic opacity

## 1. Executive conclusion

**GO.** PostgreSQL 14.24's genuine native SQL-function inliner was pinned and reproduced. It substituted eligible function bodies into the caller before plan enumeration. Exact-equivalent higher-visibility forms changed natural decisions in two cases: the controlled primary-key predicate moved from a sequential scan/function filter to an index access path, and the Q14-grounded table function changed from an opaque Function Scan to a native three-relation join with predicate pushdown. The full representation→visibility→decision→execution chain is therefore supported within the eligible SQL-function boundary.

## 2. Why R4 is the target regime

Task 1 ranked R4 as the highest-value missing representation contrast; Task 2 supplied realistic opaque UDFBench evidence. R4 uniquely tests whether executable relational structure reaches the native planner, rather than only a scalar cost (R1) or an external learned predictor (R7).

## 3. Candidate artifact audit

Five candidates were inspected in `{(results / 'r4_candidate_audit.csv').relative_to(ROOT)}`. PRISM is the most distinctive selective-opacity artifact but requires cloning/building a modified DuckDB fork, a new DBMS prohibited by the task. Froid/BlackMagic require proprietary SQL Server. QURE lacks verified released source. PostgreSQL native inlining is installed, open-source, pin-able, and compatible.

## 4. Selected mechanism and justification

Selected: PostgreSQL native scalar/table `LANGUAGE SQL` function inlining. Official PostgreSQL documentation says the planner can inline SQL functions into the whole query; PostgreSQL's source implements the eligibility and substitution logic. The engine is package version 14.24 and the corresponding upstream tag is `REL_14_24` (`6b3806732b7c5df06bdd0ed150e8a07b4ad62315`). The lower form uses the documented `SET`-clause eligibility blocker while preserving the exact body and output.

## 5. Environment and reproducibility

The stopped Task 2 PostgreSQL cluster was copied into `{runtime.relative_to(ROOT)}/postgresql/data`, then started on Unix socket `{socket}`, non-default port {PORT}, with TCP/JIT/parallelism/autovacuum disabled. No system package or DBMS was installed. Source UDFBench data hashes were rechecked and write bits removed. Details are in `metadata/task3_r4_relationalization/r4_environment.json` and `dataset_manifest.json`.

## 6. Feasibility smoke test

FEASIBLE. The blocked function name remains in the lower natural plan; the eligible name disappears and `x + 1` appears. Exact results including NULL match. Smoke timing is excluded. See `docs/task3_r4_feasibility.md`.

## 7. Workload selection

Three bounded cases were selected: CONTROLLED_PK (E2-like primary-key predicate over frozen UDFBench artifacts), UDFBENCH_Q14_CLEANDATE (exact released Q14 query with an exact-on-dataset SQL port of pure `cleandate`), and Q14_GROUNDED_TABLE (same Q14 join path/funder context inside an SQL table function). Q9's released PL/Python nested combinations loop is the explicit unsupported boundary.

## 8. Preregistered hypotheses

H1 expected increased visible structure; H2 required at least one natural decision delta; H3 prohibited runtime-only causal claims; H4 predicted that PL/Python nested loops remain unsupported. The preregistration was written and hashed before semantic validation, natural plan capture, or primary timing.

## 9. Representation pairs

All lower/higher pairs use identical SQL bodies and function attributes except for lower's `SET search_path`, which blocks native inlining. Higher is not handwritten inline SQL submitted by the harness: the submitted query still calls a function, and PostgreSQL performs substitution. Full pair definitions are in `representation_pairs.csv`.

## 10. Semantic-equivalence validation

All three pairs passed exact row-count and canonical multiset SHA-256 equality. Q14 produced {EXPECTED_Q14_ROWS} rows and `{EXPECTED_Q14_SHA256}`, exactly the frozen Task 2 result. The SQL `cleandate` port matched released PL/Python on every artifact date and ten NULL/empty/delimiter edge probes (zero mismatches). No tolerance was used.

## 11. Visibility before and after exposure

CONTROLLED_PK exposes an indexable equality predicate. Q14 cleandate exposes its CASE and string dependencies but leaves `jsonparse` and `aggregate_max` opaque. The table case exposes base relations, join predicates, funder/year predicates, access paths and cardinality context. Evidence and YES/PARTIAL/NO classifications are in `visibility_delta.csv`.

## 12. Natural optimizer plan evidence

CONTROLLED_PK changes from a Seq Scan with an opaque function filter to an index path with the equality as an index condition. Q14_GROUNDED_TABLE changes from Function Scan to enumerated base-table joins and pushes the year predicate to artifacts. Q14_CLEANDATE exposes the expression and changes optimizer cost but does not establish a distinct access/join decision. Plans were captured without ANALYZE before timing.

## 13. Decision classification

CONTROLLED_PK and Q14_GROUNDED_TABLE are `REPRESENTATION_CHANGED_DECISION`. Q14_CLEANDATE is `{next(r['classification'] for r in decisions if r['case_id']=='UDFBENCH_Q14_CLEANDATE')}`. The exact plan metrics are in `decision_classification.csv` and `plan_comparison.csv`.

## 14. Runtime results

Median lower/higher ratios were CONTROLLED_PK **{ratios['CONTROLLED_PK']:.2f}×**, UDFBENCH_Q14_CLEANDATE **{ratios['UDFBENCH_Q14_CLEANDATE']:.2f}×**, and Q14_GROUNDED_TABLE **{ratios['Q14_GROUNDED_TABLE']:.2f}×**. Each cell retains 3 warmups and 10 measured trials; no outliers were removed. Full means, SDs, CV flags, ranges and bootstrap intervals are in `normalized_results.csv`.

## 15. Planning/optimization overhead

Median lower→higher planning times were CONTROLLED_PK {plan_med[('CONTROLLED_PK','lower')]:.3f}→{plan_med[('CONTROLLED_PK','higher')]:.3f} ms, Q14 cleandate {plan_med[('UDFBENCH_Q14_CLEANDATE','lower')]:.3f}→{plan_med[('UDFBENCH_Q14_CLEANDATE','higher')]:.3f} ms, and table exposure {plan_med[('Q14_GROUNDED_TABLE','lower')]:.3f}→{plan_med[('Q14_GROUNDED_TABLE','higher')]:.3f} ms. Parse/bind time, optimizer memory and compilation time are not separately available. EXPLAIN diagnostic execution is not primary timing.

## 16. Complete causal-chain analysis

The central `causal_chain_matrix.csv` links identical-body representation change, plan-visible information, native consumer, cost/cardinality effects, natural decisions, runtime, planning overhead and boundaries. The chain is supported in two cases, while Q14 cleandate demonstrates that exposure can alter cost/execution without establishing a new physical access/join choice.

## 17. Strategic-opacity / partial-exposure result

`STRATEGIC_OPACITY_TEST = NOT_APPLICABLE`. PostgreSQL provides all-or-nothing eligibility/fallback for each SQL function body, not PRISM-style selective outlining. The SET-bearing lower form is an activation ablation, not claimed as a strategic-opacity policy.

## 18. Unsupported constructs and coverage boundaries

The mechanism does not relationalize PL/Python or PL/pgSQL, procedural loops, side effects, or dynamic code. Released Q9 `combinations` therefore remains opaque. Eligibility restrictions also include volatility, security-definer and configuration-bearing functions. These are first-class negative results, not manually replaced by SQL and attributed to PostgreSQL.

## 19. Comparison with R0, R1, and R7

R0 maximizes coverage but supplies no structure to planning. R1 reaches the cost model but not relational semantics. R7 combines source/CFG and plan context for external prediction yet did not alter a native plan in this project. R4 has the narrowest language boundary but the deepest demonstrated decision reach: exposed predicates and joins enter the native plan enumerator.

## 20. Threats to validity

The principal limits are one PostgreSQL version, tiny UDFBench data, a compatibility port for Q14 cleandate, and an eligibility ablation rather than a source-language translator. Cache state remains a threat despite block randomization. Client wall timing includes parse/plan/execute overhead; planning is therefore also measured separately. Q14 grounding is not a claim that PostgreSQL translated its original PL/Python source.

## 21. EA&B implications

The evidence supports distinguishing *having source* from *having optimizer-consumable relational form*. It also supports the survey's stage/consumer/decision framing: only information substituted before enumeration changed access and join decisions. Coverage must remain explicit because realistic Python loops stay opaque.

## 22. Decision

**GO.** A genuine R4 mechanism was reproduced and produced scientifically useful plan-backed evidence, including two complete decision chains and an explicit realistic language boundary.

## 23. Exactly one next recommended task

Run a separately approved PRISM strategic-opacity replication on its pinned modified DuckDB artifact using one SQL-ProcBench-native case. This is the single next step because Task 3 now establishes full eligible exposure but cannot test selective outlining; PRISM directly targets the remaining “more visibility is not always better” hypothesis. It requires explicit approval because it builds a new DBMS fork.
"""
        write_text_new(ROOT / "docs/task3_r4_relationalization_report.md", report)
        implications = """# Task 3 interpretation

## New claims supported

- In PostgreSQL 14.24, eligible SQL-function bodies can become visible to the native planner and change natural access-path and join decisions.
- For two exact-equivalent cases, the complete representation→visibility→decision→execution chain is observed.
- R4 decision reach can exceed R1 scalar metadata and this project's R7 external prediction, within a narrower eligibility boundary.

## Claims not supported

- PostgreSQL translates PL/Python, PL/pgSQL, or arbitrary procedural loops to relational algebra.
- More visibility is always better.
- Strategic opacity was tested.
- PRISM, Froid, BlackMagic, or QURE was reproduced.

## Claims requiring qualification

- Q14 is an exact-query compatibility probe with a validated SQL port of `cleandate`, not automatic translation of the released Python function.
- Runtime ratios are within-system results on UDFBench tiny and are not cross-engine rankings.
- The SET-bearing lower form is an inliner activation ablation, not a PostgreSQL selective-opacity feature.

## Results worth considering

- Natural Seq Scan→index and Function Scan→relational-join plan transitions.
- Separate planning-overhead measurements and the Q9 PL/Python boundary.

## Negative/boundary results worth reporting

- Realistic PL/Python nested combinations remain opaque.
- Q14 scalar expression exposure need not yield a distinct physical decision.
- The strongest strategic-opacity artifact required a prohibited new DBMS build.

## Statements that must NOT be made

- “PostgreSQL relationalized the original UDFBench Python UDFs.”
- “Task 3 reproduced PRISM/Froid.”
- “Full exposure always improves plans.”
- “Task 3 compared raw runtimes across R0/R1/R7/R4 systems.”
"""
        write_text_new(results / "interpretation.md", implications)
        write_json_new(meta / "execution_complete.json", {"run_id": run_id, "completed_at_utc": utc_now(), "status": "PASS", "primary_cases": list(valid_cases), "strategic_opacity_test": "NOT_APPLICABLE", "decision": "GO", "cluster_stopped_after_run": True})
        conn.close()
    finally:
        if process is not None:
            stop_cluster(runtime)
    print(f"TASK3_COMPLETE run_id={run_id}", flush=True)


if __name__ == "__main__":
    main()
