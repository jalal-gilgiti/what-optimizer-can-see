#!/usr/bin/env python3
"""Read-only Graceful representation and controlled-compatibility probe.

This adapter does not train or modify Graceful.  It verifies a source-level
specialization of the frozen fixed-loop kernel, invokes Graceful's released CFG
builder, and records the first unsupported representation boundary.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import traceback
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import networkx as nx
from python_graphs import control_flow
from udf_graph.create_graph import getUDFgraph


EXPERIMENTS = Path(__file__).resolve().parents[2]
if str(EXPERIMENTS) not in sys.path:
    sys.path.insert(0, str(EXPERIMENTS))

from workloads.synthetic.model import fixed_loop_checksum  # noqa: E402


if not os.environ.get("GRACEFUL_DATA_ROOT"):
    raise RuntimeError("set GRACEFUL_DATA_ROOT to the released GRACEFUL data/checkpoint directory")
RELEASED_DATA = Path(os.environ["GRACEFUL_DATA_ROOT"])
RELEASED_GRAPH = (
    RELEASED_DATA
    / "workload_runs/duckdb_pushdown/dbs/genome/created_graphs"
    / "func_14397.loopend.gpickle"
)
RELEASED_WORKLOAD = (
    RELEASED_DATA
    / "workload_runs/duckdb_pushdown/parsed_plans/genome/workload.json"
)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def specialized_source(name: str, bounds: tuple[int, ...]) -> str:
    """Generate an integer-input specialization of the frozen recursive kernel."""
    lines = [f"def {name}(value: int) -> int:", "    state = value & 2147483647"]
    indent = "    "
    for depth, bound in enumerate(bounds):
        lines.append(f"{indent}for index{depth} in range({bound}):")
        indent += "    "
        lines.append(
            f"{indent}state = (state + index{depth} + {depth}) & 2147483647"
        )
    lines.append(f"{indent}state = (state * 1103515245 + 12345) & 2147483647")
    lines.append("    return state")
    return "\n".join(lines) + "\n"


def load_values(path: Path) -> list[int]:
    metadata_path = path.with_suffix(path.suffix + ".metadata.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    observed = digest(path)
    if observed != metadata["dataset_sha256"]:
        raise RuntimeError(f"frozen dataset hash mismatch: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return [int(row["value"]) for row in csv.DictReader(handle)]


def validate_specialization(
    source: str, function_name: str, bounds: tuple[int, ...], values: list[int]
) -> dict[str, Any]:
    namespace: dict[str, Any] = {}
    exec(compile(source, f"<{function_name}>", "exec"), namespace)
    adapted = namespace[function_name]
    mismatches = []
    for index, value in enumerate(values):
        expected = fixed_loop_checksum(value, bounds)
        observed = adapted(value)
        if expected != observed:
            mismatches.append(
                {"row_index": index, "value": value, "expected": expected, "observed": observed}
            )
            if len(mismatches) == 10:
                break
    return {
        "rows_checked": len(values),
        "integer_input_cast_elided": True,
        "status": "PASS" if not mismatches else "FAIL",
        "mismatches": mismatches,
    }


def build_graph(source: str, output: Path | None) -> tuple[dict[str, Any], nx.DiGraph | None]:
    try:
        cfg = control_flow.get_control_flow_graph(source)
        graph, _ = getUDFgraph(cfg, source, is_duckdb=True, add_loop_end_node=True)
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            nx.write_gpickle(graph, output)
        counts = Counter(data["type"] for _, data in graph.nodes(data=True))
        return (
            {
                "status": "PASS",
                "node_count": graph.number_of_nodes(),
                "edge_count": graph.number_of_edges(),
                "node_types": dict(sorted(counts.items())),
                "graph_path": str(output) if output is not None else None,
            },
            graph,
        )
    except Exception as exc:
        return (
            {
                "status": "FAIL",
                "exception_type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc().splitlines(),
                "graph_path": None,
            },
            None,
        )


def recursive_plan_summary(node: dict[str, Any], counts: Counter, cards: list[dict[str, Any]]) -> None:
    params = node["plan_parameters"]
    counts[params["op_name"]] += 1
    cards.append(
        {
            key: params.get(key)
            for key in (
                "op_name",
                "act_card",
                "est_card",
                "dd_est_card",
                "wj_est_card",
                "above_udf_filter",
                "is_udf_filter",
            )
        }
    )
    for child in node.get("children", []):
        recursive_plan_summary(child, counts, cards)


def released_representation() -> dict[str, Any]:
    graph = nx.read_gpickle(RELEASED_GRAPH)
    node_types = Counter(data["type"] for _, data in graph.nodes(data=True))
    edge_types = Counter(
        f"{graph.nodes[src]['type']}->{graph.nodes[dst]['type']}" for src, dst in graph.edges
    )
    features: dict[str, set[str]] = defaultdict(set)
    for _, data in graph.nodes(data=True):
        node_type = data["type"]
        features[node_type].update(key for key in data if key != "type")

    workload = json.loads(RELEASED_WORKLOAD.read_text(encoding="utf-8"))
    matches = [
        plan
        for plan in workload["parsed_plans"]
        if plan.get("udf", {}).get("udf_name") == "func_14397"
    ]
    if not matches:
        raise RuntimeError("released func_14397 plan not found")
    plan = matches[0]
    operator_counts: Counter = Counter()
    cards: list[dict[str, Any]] = []
    recursive_plan_summary(plan, operator_counts, cards)

    return {
        "udf": "func_14397",
        "graph_path": str(RELEASED_GRAPH),
        "graph_sha256": digest(RELEASED_GRAPH),
        "graph_node_count": graph.number_of_nodes(),
        "graph_edge_count": graph.number_of_edges(),
        "node_types": dict(sorted(node_types.items())),
        "edge_types": dict(sorted(edge_types.items())),
        "feature_keys_by_node_type": {
            key: sorted(value) for key, value in sorted(features.items())
        },
        "query": plan["query"],
        "plan_runtime_ms": plan["plan_runtime_ms"],
        "relational_operator_counts": dict(sorted(operator_counts.items())),
        "relational_cardinality_fields": cards,
        "estimator_stages": [
            {
                "stage": "Python source to CFG",
                "code": "python_graphs.control_flow.get_control_flow_graph",
            },
            {
                "stage": "CFG to typed UDF graph",
                "code": "udf_graph.create_graph.getUDFgraph",
            },
            {
                "stage": "datatype and cardinality annotation",
                "code": "enhanceUDFgraph_query / enhanceUDFgraph_card",
            },
            {
                "stage": "feature encoding and SQL/UDF graph joining",
                "code": "dd_plan_batching.udf_to_graph / duckdb_plan_collator",
            },
            {
                "stage": "runtime estimation",
                "code": "zero-shot GNN forward pass and label inverse transform",
            },
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lambda1-graph", type=Path, required=True)
    args = parser.parse_args()

    fixed_dataset = EXPERIMENTS / "workloads/synthetic/generated/fixed_loop_r10000_rho1p0_seed42.csv"
    selective_dataset = (
        EXPERIMENTS
        / "workloads/synthetic/generated/relational_selectivity_r10000_rho0p1_seed42.csv"
    )
    values_fixed = load_values(fixed_dataset)
    values_selective = load_values(selective_dataset)

    cases = []
    for name, bounds, values, dataset in (
        ("e5_fixed_l1", (1,), values_fixed, fixed_dataset),
        ("e5_fixed_l960", (8, 10, 12), values_fixed, fixed_dataset),
        ("e5_e2_l960", (8, 10, 12), values_selective, selective_dataset),
    ):
        source = specialized_source(name, bounds)
        semantic = validate_specialization(source, name, bounds, values)
        graph_output = args.lambda1_graph if bounds == (1,) else None
        graph_result, _ = build_graph(source, graph_output)
        cases.append(
            {
                "case": name,
                "bounds": list(bounds),
                "Lambda": 1 if bounds == (1,) else 960,
                "dataset": str(dataset),
                "dataset_sha256": digest(dataset),
                "source": source,
                "semantic_equivalence": semantic,
                "graceful_graph_build": graph_result,
            }
        )

    incompatible = any(
        case["graceful_graph_build"]["status"] != "PASS" for case in cases
    )
    payload = {
        "schema_version": "1.0.0",
        "released_representation": released_representation(),
        "controlled_cases": cases,
        "controlled_workload_status": (
            "INCOMPATIBLE_WITH_RELEASED_CHECKPOINT" if incompatible else "PASS"
        ),
        "stop_reason": (
            "The released graph builder cannot featurize the exact three-level Lambda=960 loop; flattening it would change the source representation."
            if incompatible
            else None
        ),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 2 if incompatible else 0


if __name__ == "__main__":
    raise SystemExit(main())
