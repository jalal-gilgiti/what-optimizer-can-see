#!/usr/bin/env python3
"""Prepare and validate E5b inputs without training or changing Graceful."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import networkx as nx
from python_graphs import control_flow
from udf_graph.annotate_graph_info import enhanceUDFgraph_query
from udf_graph.create_graph import getUDFgraph


EXPERIMENTS = Path(__file__).resolve().parents[2]
if str(EXPERIMENTS) not in sys.path:
    sys.path.insert(0, str(EXPERIMENTS))

from workloads.synthetic.model import fixed_loop_checksum  # noqa: E402


RUN_ID = "e5b-graceful-20260830T134817Z"
RUNTIME = EXPERIMENTS / "runtime/graceful" / RUN_ID
WORKLOAD = RUNTIME / "workload"
FIXED = EXPERIMENTS / "workloads/synthetic/generated/fixed_loop_r10000_rho1p0_seed42.csv"
SELECTIVE = EXPERIMENTS / "workloads/synthetic/generated/relational_selectivity_r10000_rho0p1_seed42.csv"
E1_RESULTS = EXPERIMENTS / "results/normalized/e1_duckdb.csv"
E2_RESULTS = EXPERIMENTS / "results/normalized/e2_duckdb.csv"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_values(path: Path) -> list[int]:
    metadata = json.loads(path.with_suffix(path.suffix + ".metadata.json").read_text())
    if sha256(path) != metadata["dataset_sha256"]:
        raise RuntimeError(f"frozen dataset hash mismatch: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return [int(row["value"]) for row in csv.DictReader(handle)]


def measured_e1(lambda_value: int) -> float:
    with E1_RESULTS.open(newline="", encoding="utf-8") as handle:
        rows = [row for row in csv.DictReader(handle)
                if int(row["R"]) == 10000 and int(row["Lambda"]) == lambda_value]
    if len(rows) != 1:
        raise RuntimeError(f"expected one frozen E1 row for Lambda={lambda_value}")
    return float(rows[0]["median_runtime"])


def measured_e2(form: str) -> float:
    with E2_RESULTS.open(newline="", encoding="utf-8") as handle:
        rows = [row for row in csv.DictReader(handle)
                if int(row["R"]) == 10000 and float(row["rho"]) == 0.1
                and int(row["Lambda"]) == 100 and row["form"] == form]
    if len(rows) != 1:
        raise RuntimeError(f"expected one frozen E2 row for {form}")
    return float(rows[0]["median_runtime"])


def compatible_source(name: str, bounds: tuple[int, ...]) -> str:
    """Exact-on-frozen-input specialization using the checkpoint vocabulary.

    The power-of-two mask is expressed with math.floor and Div/Mult/Sub because
    the released checkpoint has no BitAnd token. Equivalence is checked against
    the frozen kernel for every row before an artifact is accepted.
    """
    lines = [f"def {name}(value: int) -> int:", "    import math", "    state = value"]
    indent = "    "
    for depth, bound in enumerate(bounds):
        lines.append(f"{indent}for index{depth} in range({bound}):")
        indent += "    "
        lines.append(f"{indent}state = state + index{depth} + {depth}")
        lines.append(
            f"{indent}state = state - math.floor(state / 2147483648) * 2147483648"
        )
    lines.append(f"{indent}state = state * 1103515245 + 12345")
    lines.append(
        f"{indent}state = state - math.floor(state / 2147483648) * 2147483648"
    )
    lines.append("    return state")
    return "\n".join(lines) + "\n"


def semantic_check(source: str, name: str, bounds: tuple[int, ...], values: list[int]) -> dict[str, Any]:
    namespace: dict[str, Any] = {}
    exec(compile(source, f"<{name}>", "exec"), namespace)
    function = namespace[name]
    mismatches = []
    for index, value in enumerate(values):
        expected = fixed_loop_checksum(value, bounds)
        observed = function(value)
        if observed != expected:
            mismatches.append({"row": index, "value": value, "expected": expected, "observed": observed})
            if len(mismatches) == 10:
                break
    return {
        "status": "PASS" if not mismatches else "FAIL",
        "rows_checked": len(values),
        "mismatches": mismatches,
        "equivalence_scope": "all rows of the exact frozen input",
    }


def graph_json(graph: nx.DiGraph) -> dict[str, Any]:
    nodes = []
    for node_id, data in graph.nodes(data=True):
        cleaned = {}
        for key, value in data.items():
            if key == "lib_embedding":
                cleaned["lib_embedding_dimension"] = len(value)
                cleaned["lib_embedding_nonzero"] = int((value != 0).sum())
            else:
                cleaned[key] = value.item() if hasattr(value, "item") else value
        nodes.append({"id": node_id, **cleaned})
    return {
        "node_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "node_types": dict(sorted(Counter(d["type"] for _, d in graph.nodes(data=True)).items())),
        "nodes": nodes,
        "edges": [{"source": source, "target": target} for source, target in graph.edges()],
    }


def make_graph(name: str, bounds: tuple[int, ...], input_cardinality: int,
               values: list[int], dataset: Path, dataset_name: str) -> tuple[dict[str, Any], Path]:
    source = compatible_source(name, bounds)
    semantic = semantic_check(source, name, bounds, values)
    if semantic["status"] != "PASS":
        raise RuntimeError(f"semantic specialization failed for {name}")
    graph, _ = getUDFgraph(
        control_flow.get_control_flow_graph(source), source,
        is_duckdb=True, add_loop_end_node=True,
    )
    graph = enhanceUDFgraph_query(graph, name, source.splitlines()[0], dbms="duckdb")
    for node, data in graph.nodes(data=True):
        if data["type"] != "VAR":
            for suffix in ("act", "est", "deepdb", "wj"):
                graph.nodes[node][f"in_rows_{suffix}"] = input_cardinality
    output = WORKLOAD / "dbs" / dataset_name / "created_graphs" / f"{name}.loopend.gpickle"
    output.parent.mkdir(parents=True, exist_ok=True)
    nx.write_gpickle(graph, output)
    inspection = graph_json(graph)
    inspection.update({
        "udf_name": name,
        "bounds": list(bounds),
        "Lambda": 1 if bounds == (1,) else 100,
        "source": source,
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "dataset": str(dataset),
        "dataset_sha256": sha256(dataset),
        "semantic_equivalence": semantic,
        "input_cardinality": input_cardinality,
        "graph_path": str(output),
        "graph_sha256": sha256(output),
    })
    return inspection, output


def cards(op_name: str, act: int, children: int, *, above: bool = False) -> dict[str, Any]:
    return {
        "op_name": op_name,
        "act_card": act, "est_card": act, "dd_est_card": act, "wj_est_card": act,
        "act_children_card": children, "est_children_card": children,
        "dd_est_children_card": children, "wj_est_children_card": children,
        "above_udf_filter": above, "is_udf_filter": False,
    }


def scan(act: int, with_filter: bool = False) -> dict[str, Any]:
    params = cards("SEQ_SCAN", act, 1)
    params.update({
        "table_name": "input_rows", "table": 0,
        "output_columns": [
            {"aggregation": "None", "columns": [1], "udf_name": None,
             "udf_output": "False", "child_ref": False}
        ],
    })
    if with_filter:
        params["output_columns"].append(
            {"aggregation": "None", "columns": [2], "udf_name": None,
             "udf_output": "False", "child_ref": False}
        )
        params["filter_columns"] = {
            "column": 2, "operator": "=", "literal": "1", "literal_feature": 0,
            "children": [], "udf_name": None, "text": "retained = 1",
        }
    return {"plan_parameters": params, "children": []}


def udf_output(name: str) -> dict[str, Any]:
    return {
        "aggregation": "SUM", "columns": None, "udf_name": name,
        "udf_output": "True", "child_ref": False,
    }


def plan_e1(name: str, runtime_ms: float, bounds: tuple[int, ...]) -> dict[str, Any]:
    root = cards("UNGROUPED_AGGREGATE", 1, 10000)
    root.update({"udf_table": 0, "udf_params": [1], "output_columns": [udf_output(name)]})
    return {
        "plan_parameters": root, "children": [scan(10000)],
        "udf": {"udf_name": name, "udf_math_lib_imported": True,
                "udf_numpy_lib_imported": False, "udf_num_math_calls": len(bounds) + 1,
                "udf_num_np_calls": 0, "udf_pos_in_query": "select",
                "udf_num_loops": len(bounds), "udf_num_branches": 0},
        "query": f"SELECT SUM({name}(value)) FROM input_rows",
        "plan_runtime_ms": runtime_ms, "num_tables": 1, "num_filters": 0,
    }


def plan_e2_filter_before(name: str, runtime_ms: float) -> dict[str, Any]:
    root = cards("UNGROUPED_AGGREGATE", 1, 1000)
    root.update({"udf_table": 0, "udf_params": [1], "output_columns": [udf_output(name)]})
    return {
        "plan_parameters": root, "children": [scan(1000, with_filter=True)],
        "udf": {"udf_name": name, "udf_math_lib_imported": True,
                "udf_numpy_lib_imported": False, "udf_num_math_calls": 3,
                "udf_num_np_calls": 0, "udf_pos_in_query": "select",
                "udf_num_loops": 2, "udf_num_branches": 0},
        "query": f"SELECT SUM({name}(value)) FROM input_rows WHERE retained = 1",
        "plan_runtime_ms": runtime_ms, "num_tables": 1, "num_filters": 1,
    }


def plan_e2_udf_before(name: str, runtime_ms: float) -> dict[str, Any]:
    root = cards("UNGROUPED_AGGREGATE", 1, 1000, above=True)
    filter_params = cards("FILTER", 1000, 10000, above=True)
    filter_params["filter_columns"] = {
        "column": 2, "operator": "=", "literal": "1", "literal_feature": 0,
        "children": [], "udf_name": None, "text": "retained = 1",
    }
    projection = cards("PROJECTION", 10000, 10000)
    projection.update({"udf_table": 0, "udf_params": [1], "output_columns": [udf_output(name)]})
    tree = {
        "plan_parameters": root,
        "children": [{"plan_parameters": filter_params, "children": [
            {"plan_parameters": projection, "children": [scan(10000)]}
        ]}],
    }
    tree.update({
        "udf": {"udf_name": name, "udf_math_lib_imported": True,
                "udf_numpy_lib_imported": False, "udf_num_math_calls": 3,
                "udf_num_np_calls": 0, "udf_pos_in_query": "select",
                "udf_num_loops": 2, "udf_num_branches": 0},
        "query": f"SELECT SUM(v) FROM (SELECT {name}(value) AS v, retained FROM input_rows OFFSET 0) t WHERE retained = 1",
        "plan_runtime_ms": runtime_ms, "num_tables": 1, "num_filters": 1,
    })
    return tree


def database_stats() -> dict[str, Any]:
    return {
        "column_stats": [
            {"table_name": "input_rows", "column_name": "id", "data_type": "BIGINT", "table_size": 10000},
            {"table_name": "input_rows", "column_name": "value", "data_type": "BIGINT", "table_size": 10000},
            {"table_name": "input_rows", "column_name": "retained", "data_type": "BIGINT", "table_size": 10000},
        ],
        "table_stats": [{"table_name": "input_rows", "estimated_size": 10000}],
    }


def write_workload(dataset_name: str, plans: list[dict[str, Any]]) -> Path:
    path = WORKLOAD / "parsed_plans" / dataset_name / "workload.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"parsed_plans": plans, "database_stats": database_stats(),
               "run_kwargs": {"hardware": "c8220"}}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    blacklist = WORKLOAD / "dbs" / dataset_name / "created_graphs/udf_w_col_col_comparison.json"
    blacklist.write_text("[]\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("e1", "e2"))
    parser.add_argument("--inspection", type=Path, required=True)
    args = parser.parse_args()

    values_fixed = load_values(FIXED)
    values_selective = load_values(SELECTIVE)
    inspections = []
    if args.phase == "e1":
        cases = [
            ("e5b_e1_l1", (1,), 10000, values_fixed, FIXED),
            ("e5b_e1_l100", (10, 10), 10000, values_fixed, FIXED),
        ]
        plans = []
        for name, bounds, card, values, dataset in cases:
            inspection, _ = make_graph(name, bounds, card, values, dataset, "e5b_e1")
            inspections.append(inspection)
            plans.append(plan_e1(name, measured_e1(inspection["Lambda"]), bounds))
        workload_path = write_workload("e5b_e1", plans)
    else:
        cases = [
            ("e5b_e2_l100_filter_before", 1000, "filter_before_udf"),
            ("e5b_e2_l100_udf_before", 10000, "udf_before_filter"),
        ]
        plans = []
        for name, card, form in cases:
            inspection, _ = make_graph(name, (10, 10), card, values_selective, SELECTIVE, "e5b_e2")
            inspection["placement_form"] = form
            inspections.append(inspection)
            if form == "filter_before_udf":
                plans.append(plan_e2_filter_before(name, measured_e2(form)))
            else:
                plans.append(plan_e2_udf_before(name, measured_e2(form)))
        workload_path = write_workload("e5b_e2", plans)

    args.inspection.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0.0", "run_id": RUN_ID, "phase": args.phase,
        "workload_path": str(workload_path), "workload_sha256": sha256(workload_path),
        "representations": inspections,
    }
    args.inspection.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "phase": args.phase, "workload": str(workload_path),
                      "inspection": str(args.inspection)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
