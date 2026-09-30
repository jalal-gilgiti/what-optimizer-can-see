#!/usr/bin/env python3
"""Generate the exact-stratified, run-specific E4 datasets."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
R = 10_000
L = 10
H_VALUES = (20, 100, 1_000)
CORRELATIONS = ("positive", "negative", "independent")
SEED = 42


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def qerror(estimate: float, actual: float) -> float:
    return max(estimate / actual, actual / estimate)


def assignments(high: int, correlation: str, assignment_order: list[int]) -> dict[int, tuple[int, int]]:
    if correlation == "positive":
        cells = [(1, high)] * 5_000 + [(0, L)] * 5_000
    elif correlation == "negative":
        cells = [(1, L)] * 5_000 + [(0, high)] * 5_000
    elif correlation == "independent":
        cells = (
            [(1, L)] * 2_500
            + [(1, high)] * 2_500
            + [(0, L)] * 2_500
            + [(0, high)] * 2_500
        )
    else:
        raise ValueError(correlation)
    return {row_id: cell for row_id, cell in zip(assignment_order, cells, strict=True)}


def summarize(rows: list[dict[str, int]], high: int, correlation: str) -> dict[str, Any]:
    joint = {
        f"Z={z},I={iterations}": sum(row["flag"] == z and row["loop_count"] == iterations for row in rows)
        for z in (0, 1)
        for iterations in (L, high)
    }
    active = sum(row["flag"] for row in rows)
    low_count = sum(row["loop_count"] == L for row in rows)
    high_count = sum(row["loop_count"] == high for row in rows)
    true_work = sum(row["flag"] * row["loop_count"] for row in rows)
    active_work = sum(row["loop_count"] for row in rows if row["flag"])
    e_i = sum(row["loop_count"] for row in rows) / R
    e_i_active = active_work / active
    m1 = R * (active / R) * e_i
    m2 = R * (active / R) * e_i_active
    expected_true = {
        "positive": R * high / 2,
        "negative": R * L / 2,
        "independent": R * (L + high) / 4,
    }[correlation]
    expected_m1_q = {
        "positive": 2 * high / (L + high),
        "negative": (L + high) / (2 * L),
        "independent": 1.0,
    }[correlation]
    if true_work != expected_true:
        raise RuntimeError(f"true work mismatch for H={high}, {correlation}")
    if qerror(m1, true_work) != expected_m1_q or qerror(m2, true_work) != 1.0:
        raise RuntimeError(f"analytical expectation mismatch for H={high}, {correlation}")
    return {
        "R": R,
        "L": L,
        "H": high,
        "correlation": correlation,
        "seed": SEED,
        "joint_cell_counts": joint,
        "z_active_count": active,
        "i_low_count": low_count,
        "i_high_count": high_count,
        "p_z_active": active / R,
        "p_i_low": low_count / R,
        "p_i_high": high_count / R,
        "e_i": e_i,
        "e_i_given_z_active": e_i_active,
        "true_work": true_work,
        "m1_predicted_work": m1,
        "m1_qerror": qerror(m1, true_work),
        "m2_predicted_work": m2,
        "m2_qerror": qerror(m2, true_work),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    output_dir = ROOT / "workloads" / "synthetic" / "generated" / args.run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    entries = []
    for high in H_VALUES:
        values_rng = random.Random(SEED * 1_000_003 + high)
        base_values = {row_id: values_rng.randint(1, 1_000_000) for row_id in range(R)}
        assignment_order = list(range(R))
        random.Random(SEED * 10_007 + high).shuffle(assignment_order)
        output_order = list(range(R))
        random.Random(SEED * 100_003 + high).shuffle(output_order)
        for correlation in CORRELATIONS:
            assigned = assignments(high, correlation, assignment_order)
            rows = []
            for row_id in output_order:
                flag, loop_count = assigned[row_id]
                rows.append({
                    "id": row_id,
                    "value": base_values[row_id],
                    "flag": flag,
                    "loop_count": loop_count,
                    "retained": 1,
                    "lookup_key": row_id % 16,
                })
            summary = summarize(rows, high, correlation)
            path = output_dir / f"e4_h{high}_{correlation}_seed{SEED}.csv"
            with path.open("x", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=("id", "value", "flag", "loop_count", "retained", "lookup_key"))
                writer.writeheader()
                writer.writerows(rows)
            summary["dataset_path"] = str(path.resolve())
            summary["dataset_sha256"] = sha256(path)
            entries.append(summary)

    for high in H_VALUES:
        group = [entry for entry in entries if entry["H"] == high]
        marginal_keys = ("R", "z_active_count", "i_low_count", "i_high_count", "e_i")
        if any(len({entry[key] for entry in group}) != 1 for key in marginal_keys):
            raise RuntimeError(f"matched marginal validation failed for H={high}")
        ratios = {entry["correlation"]: entry["true_work"] for entry in group}
        if ratios["positive"] / ratios["negative"] != high / L:
            raise RuntimeError(f"work-ratio validation failed for H={high}")

    manifest = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "generator": str(Path(__file__).resolve()),
        "generator_sha256": sha256(Path(__file__).resolve()),
        "algorithm": (
            "Per H: deterministic shared value vector; shared shuffled assignment ID order; "
            "exact cell labels; shared independently shuffled output ID order."
        ),
        "dataset_count": len(entries),
        "matched_marginals": True,
        "datasets": entries,
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.manifest.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps({"run_id": args.run_id, "datasets": len(entries), "matched_marginals": True}, sort_keys=True))


if __name__ == "__main__":
    main()
