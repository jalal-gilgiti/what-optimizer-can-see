#!/usr/bin/env python3
"""Consolidate the already-captured E5b Graceful probe artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path


EXP = Path(__file__).resolve().parents[2]
RUN_ID = "e5b-graceful-20260830T134817Z"
RAW = EXP / "results/raw" / RUN_ID
PLANS = EXP / "results/plans" / RUN_ID
NORMALIZED = EXP / "results/normalized" / RUN_ID
METADATA = EXP / "metadata" / RUN_ID
DOC = EXP / "docs" / f"e5b_graceful_compatible_{RUN_ID}.md"
if not os.environ.get("GRACEFUL_DATA_ROOT"):
    raise RuntimeError("set GRACEFUL_DATA_ROOT to the released GRACEFUL data/checkpoint directory")
GRACEFUL_DATA_ROOT = Path(os.environ["GRACEFUL_DATA_ROOT"])
CHECKPOINT = GRACEFUL_DATA_ROOT / "graceful_artifacts/leave_out_genome/act_minrt50ms_complex_dd_wpullupdata_wnoUDFdata_ddactfonudf_liboh_gradnorm_mldupl_loopend_loopedge_genome_stratDBs_ep60_maxr30_20240503_115437_074.pt"
STATS = GRACEFUL_DATA_ROOT / "workload_runs/duckdb_pushdown/parsed_plans/statistics_workload_combined.json"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def exact_truths() -> dict[str, float]:
    truths: dict[str, float] = {}
    with (EXP / "results/normalized/e1_duckdb.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if int(row["R"]) == 10000 and int(row["Lambda"]) in (1, 100):
                truths[f"e1_l{row['Lambda']}"] = float(row["median_runtime"])
    with (EXP / "results/normalized/e2_duckdb.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if (int(row["R"]) == 10000 and float(row["rho"]) == 0.1
                    and int(row["Lambda"]) == 100):
                truths[f"e2_{row['form']}"] = float(row["median_runtime"])
    return truths


def main() -> int:
    NORMALIZED.mkdir(parents=True, exist_ok=True)
    METADATA.mkdir(parents=True, exist_ok=True)
    DOC.parent.mkdir(parents=True, exist_ok=True)

    e1_repr = read_json(PLANS / "representation_e1.json")
    e2_repr = read_json(PLANS / "representation_e2.json")
    e1_raw = read_json(RAW / "e1_predictions.json")
    e2_raw = read_json(RAW / "e2_predictions.json")
    truths = exact_truths()

    cases = [
        ("E1", "e1_l1", 1, "[1]", "single_form", e1_repr["representations"][0], e1_raw, 0),
        ("E1", "e1_l100", 100, "[10,10]", "single_form", e1_repr["representations"][1], e1_raw, 1),
        ("E2", "e2_filter_before_udf", 100, "[10,10]", "filter_before_udf", e2_repr["representations"][0], e2_raw, 0),
        ("E2", "e2_udf_before_filter", 100, "[10,10]", "udf_before_filter", e2_repr["representations"][1], e2_raw, 1),
    ]
    rows = []
    for experiment, case_id, lam, bounds, form, representation, raw, index in cases:
        prediction_ms = float(raw["predictions_seconds"][index]) * 1000
        measured_ms = truths[case_id]
        q_error = max(prediction_ms / measured_ms, measured_ms / prediction_ms)
        batch = raw["batch_stats"]
        rows.append({
            "run_id": RUN_ID,
            "experiment": experiment,
            "case_id": case_id,
            "R": 10000,
            "rho": "" if experiment == "E1" else 0.1,
            "Lambda": lam,
            "loop_bounds": bounds,
            "form": form,
            "compatibility": "PASS",
            "predicted_runtime_ms": f"{prediction_ms:.9f}",
            "frozen_measured_runtime_ms": f"{measured_ms:.9f}",
            "q_error": f"{q_error:.9f}",
            "source_graph_nodes": representation["node_count"],
            "source_graph_edges": representation["edge_count"],
            "joint_graph_nodes": batch["graph_num_nodes"][index],
            "joint_graph_edges": batch["graph_num_edges"][index],
            "udf_input_cardinality": batch["udf_in_card"][index],
            "loop_head_nodes": representation["node_types"].get("LOOP_HEAD", 0),
            "loop_end_nodes": representation["node_types"].get("LOOP_END", 0),
            "branch_nodes": representation["node_types"].get("BRANCH", 0),
            "checkpoint_sha256": digest(CHECKPOINT),
        })

    csv_path = NORMALIZED / "predictions.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    representation_path = PLANS / "representation_inspection.json"
    representation_payload = {
        "schema_version": "1.0.0",
        "run_id": RUN_ID,
        "status": "PASS",
        "procedural_encoding": {
            "node_types": ["INVOCATION", "COMP", "LOOP_HEAD", "LOOP_END", "RETURN"],
            "branch_nodes_relevant": False,
            "branch_nodes_observed": 0,
            "loop_features": ["loop_type", "fixed_iter", "no_iter", "loop_part", "in_rows_act"],
            "operation_tokens_observed": ["Add", "Div", "Mult", "Sub", "math.floor"],
            "lambda_1_bounds": [1],
            "lambda_100_bounds": [10, 10],
            "source_specialization": "The frozen power-of-two mask is expressed with math.floor and released Div/Mult/Sub tokens; all 10,000 exact frozen rows passed output equivalence for each dataset/case.",
        },
        "relational_encoding": {
            "e1_operators": ["UNGROUPED_AGGREGATE", "SEQ_SCAN"],
            "e2_filter_before_operators": ["UNGROUPED_AGGREGATE", "SEQ_SCAN"],
            "e2_udf_before_operators": ["UNGROUPED_AGGREGATE", "FILTER", "PROJECTION", "SEQ_SCAN"],
            "features": ["act_card", "act_children_card", "op_name", "estimated_size", "data_type", "aggregation", "udf_output", "operator", "literal_feature", "on_udf"],
            "input_rows": 10000,
            "e2_retained_rows": 1000,
        },
        "e1": e1_repr,
        "e2": e2_repr,
        "captured_joint_graphs": {
            "e1": {key: e1_raw[key] for key in ("batch_graph_total_nodes", "batch_graph_total_edges", "batch_graph_node_counts", "batch_graph_edge_counts", "batch_stats")},
            "e2": {key: e2_raw[key] for key in ("batch_graph_total_nodes", "batch_graph_total_edges", "batch_graph_node_counts", "batch_graph_edge_counts", "batch_stats")},
        },
        "representation_boundary": {
            "supported_two_level_loop": True,
            "unsupported_three_level_loop": True,
            "three_level_evidence": "Frozen E5 run e5-graceful-20260830T064548Z: AssertionError: a third while loop is not supported in the featurization",
            "lambda_960_executed_in_e5b": False,
        },
    }
    representation_path.write_text(json.dumps(representation_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    validation_path = METADATA / "validation_metadata.json"
    validation = {
        "schema_version": "1.0.0",
        "run_id": RUN_ID,
        "scope": "E5b Graceful-compatible controlled probe only",
        "status": "PASS",
        "decision": "CONDITIONAL GO",
        "source_commit": "df053f641722102f9f9a07162e58665a9ecfb40b",
        "source_clean_after_execution": True,
        "reused_environment": "experiments/runtime/graceful/e5-graceful-20260830T064548Z/env",
        "checkpoint": {"path": str(CHECKPOINT), "sha256": digest(CHECKPOINT), "epoch": 60},
        "feature_statistics": {"path": str(STATS), "sha256": digest(STATS)},
        "training_or_finetuning": False,
        "graceful_featurization_modified": False,
        "new_system_started": False,
        "third_loop_tested": False,
        "lambda_960_tested": False,
        "frozen_datasets_regenerated": False,
        "semantic_equivalence": {"status": "PASS", "rows_per_case": 10000},
        "compatibility": {"lambda_1": "PASS", "lambda_100": "PASS", "e2_pair": "SUPPORTED"},
        "source_ablation": {
            "status": "UNSUPPORTED",
            "reason": "The source documents training configurations for general ablations, but the pinned released artifact set contains no matched clean source-structure ablation checkpoint. Applying mp_ignore_udf at inference to the full checkpoint would not be a clean released ablation and was not run.",
        },
        "decision_reason": "All requested representations and predictions succeeded, but Lambda=1 Q-error is 16.4378 and Lambda=100 E1 Q-error is 2.2113; predictive use should remain conditional despite compatibility.",
        "artifacts": {
            "normalized_predictions": str(csv_path.relative_to(ROOT)),
            "raw_e1": str((RAW / "e1_predictions.json").relative_to(ROOT)),
            "raw_e2": str((RAW / "e2_predictions.json").relative_to(ROOT)),
            "representation_inspection": str(representation_path.relative_to(ROOT)),
        },
    }
    validation_path.write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    by_id = {row["case_id"]: row for row in rows}
    report = f"""# E5b Graceful-compatible controlled probe — {RUN_ID}

## 1. Lambda=1 compatibility

**PASS.** Predicted {float(by_id['e1_l1']['predicted_runtime_ms']):.3f} ms; frozen measured runtime {float(by_id['e1_l1']['frozen_measured_runtime_ms']):.3f} ms.

## 2. Lambda=100 compatibility

**PASS.** The released featurizer consumed the exact two-level bounds `(10,10)`. Predicted {float(by_id['e1_l100']['predicted_runtime_ms']):.3f} ms; frozen measured runtime {float(by_id['e1_l100']['frozen_measured_runtime_ms']):.3f} ms.

## 3. Q-error for each supported case

| Case | Predicted ms | Frozen measured ms | Q-error |
|---|---:|---:|---:|
| E1 Lambda=1 | {float(by_id['e1_l1']['predicted_runtime_ms']):.3f} | {float(by_id['e1_l1']['frozen_measured_runtime_ms']):.3f} | {float(by_id['e1_l1']['q_error']):.4f} |
| E1 Lambda=100 | {float(by_id['e1_l100']['predicted_runtime_ms']):.3f} | {float(by_id['e1_l100']['frozen_measured_runtime_ms']):.3f} | {float(by_id['e1_l100']['q_error']):.4f} |
| E2 filter_before_udf | {float(by_id['e2_filter_before_udf']['predicted_runtime_ms']):.3f} | {float(by_id['e2_filter_before_udf']['frozen_measured_runtime_ms']):.3f} | {float(by_id['e2_filter_before_udf']['q_error']):.4f} |
| E2 udf_before_filter | {float(by_id['e2_udf_before_filter']['predicted_runtime_ms']):.3f} | {float(by_id['e2_udf_before_filter']['frozen_measured_runtime_ms']):.3f} | {float(by_id['e2_udf_before_filter']['q_error']):.4f} |

## 4. Procedural features actually encoded

`INVOCATION`, `COMP`, `LOOP_HEAD`, `LOOP_END`, and `RETURN`; `loop_type=for`, `fixed_iter=True`, `no_iter=1` or two encoded bounds of `10`, `loop_part`, exact UDF input cardinality, input/output integer datatypes, and released operation/library tokens `Add`, `Div`, `Mult`, `Sub`, and `math.floor`. No branch exists in this workload, so encoded branch count is zero. Source graphs: Lambda=1 is 9 nodes/9 edges; Lambda=100 is 13 nodes/14 edges. Joint SQL/UDF graphs: 14/15 and 16/20 nodes/edges.

The original power-of-two mask was expressed as an output-equivalent `math.floor` arithmetic specialization because `BitAnd` is absent from the checkpoint vocabulary. Every one of the 10,000 exact frozen values was checked against the frozen kernel for every case; Graceful itself was not modified.

## 5. E2 pair

**SUPPORTED.** Relational/cardinality features include the aggregate, scan, filter and projection topology, actual/input-child cardinalities, table size, BIGINT column type, filter operator/literal, UDF output aggregation, 10,000 input rows, and 1,000 retained rows.

## 6. E2 prediction result

Filter-before-UDF: {float(by_id['e2_filter_before_udf']['predicted_runtime_ms']):.3f} ms, Q-error {float(by_id['e2_filter_before_udf']['q_error']):.4f}. UDF-before-filter: {float(by_id['e2_udf_before_filter']['predicted_runtime_ms']):.3f} ms, Q-error {float(by_id['e2_udf_before_filter']['q_error']):.4f}.

## 7. Source ablation

**UNSUPPORTED.** The source documents configurations for training general ablations, but the pinned released artifact set has no matched clean source-structure ablation checkpoint. Applying `mp_ignore_udf` only at inference to the full checkpoint would not be a clean released ablation, so none was invented or run.

## 8. Representation boundary

- Supported two-level loop: **YES** — `(10,10)` was featurized and inferred.
- Unsupported three-level loop: **YES** — retained from frozen E5 evidence: `AssertionError: a third while loop is not supported in the featurization`. E5b did not run a third loop or Lambda=960.

## 9. Decision

**CONDITIONAL GO.** Representation compatibility and E2 placement sensitivity pass, but the E1 Q-errors—especially 16.4378 at Lambda=1—do not support unconditional predictive use.

## 10. Key paths

- Normalized predictions: `{csv_path.relative_to(ROOT)}`
- Raw E1 predictions: `{(RAW / 'e1_predictions.json').relative_to(ROOT)}`
- Raw E2 predictions: `{(RAW / 'e2_predictions.json').relative_to(ROOT)}`
- Representation inspection: `{representation_path.relative_to(ROOT)}`
- Validation metadata: `{validation_path.relative_to(ROOT)}`
- Frozen E1/E2 workloads: `experiments/workloads/synthetic/generated/`
- Reused pinned Graceful source/environment: `experiments/runtime/graceful/e5-graceful-20260830T064548Z/`
"""
    DOC.write_text(report, encoding="utf-8")

    validation["artifacts"].update({
        "report": str(DOC.relative_to(ROOT)),
        "report_sha256": digest(DOC),
        "normalized_predictions_sha256": digest(csv_path),
        "representation_inspection_sha256": digest(representation_path),
        "raw_e1_sha256": digest(RAW / "e1_predictions.json"),
        "raw_e2_sha256": digest(RAW / "e2_predictions.json"),
    })
    validation_path.write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "report": str(DOC), "csv": str(csv_path),
                      "metadata": str(validation_path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
