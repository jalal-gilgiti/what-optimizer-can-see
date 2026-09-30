#!/usr/bin/env python3
"""Generate small, deterministic datasets that isolate procedural variables."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
from pathlib import Path
from typing import Any

from workloads.synthetic.model import amplification


FAMILIES = (
    "fixed_loop",
    "dynamic_loop",
    "branch",
    "branch_loop",
    "relational_selectivity",
    "data_access",
    "udf_chain",
    "opaque_call",
)


def exact_flags(count: int, probability: float, rng: random.Random) -> list[int]:
    active = min(count, max(0, round(count * probability)))
    flags = [1] * active + [0] * (count - active)
    rng.shuffle(flags)
    return flags


def loop_counts(count: int, distribution: str, base: int, rng: random.Random) -> list[int]:
    if distribution == "constant":
        return [base] * count
    if distribution == "uniform":
        return [rng.randint(0, max(1, base * 2)) for _ in range(count)]
    if distribution == "skewed":
        return [min(base * 8, int(rng.expovariate(1 / max(1, base)))) for _ in range(count)]
    if distribution == "bimodal":
        return [max(0, base // 4) if rng.random() < 0.7 else base * 3 for _ in range(count)]
    raise ValueError(f"unsupported distribution: {distribution}")


def correlate_flags(counts: list[int], probability: float, mode: str, rng: random.Random) -> list[int]:
    active = min(len(counts), max(0, round(len(counts) * probability)))
    if mode == "independent":
        return exact_flags(len(counts), probability, rng)
    order = sorted(range(len(counts)), key=lambda index: (counts[index], index))
    selected = order[-active:] if mode == "positive" and active else order[:active]
    flags = [0] * len(counts)
    for index in selected:
        flags[index] = 1
    return flags


def summarize(rows: list[dict[str, int]], bounds: tuple[int, ...]) -> dict[str, Any]:
    size = len(rows)
    branch_count = sum(row["flag"] for row in rows)
    iterations = [row["loop_count"] for row in rows]
    branch_iterations = [row["loop_count"] for row in rows if row["flag"]]
    return {
        "row_count": size,
        "loop_bounds": list(bounds),
        "Lambda": amplification(bounds),
        "p_branch_realized": branch_count / size if size else None,
        "e_i_realized": sum(iterations) / size if size else None,
        "e_i_given_branch_realized": (
            sum(branch_iterations) / len(branch_iterations) if branch_iterations else None
        ),
        "e_branch_times_i_realized": (
            sum(row["flag"] * row["loop_count"] for row in rows) / size if size else None
        ),
    }


def generate(args: argparse.Namespace) -> tuple[list[dict[str, int]], dict[str, Any]]:
    if args.rows < 0:
        raise ValueError("--rows must be non-negative")
    rng = random.Random(args.seed)
    bounds = tuple(args.loop_bounds)
    counts = loop_counts(args.rows, args.distribution, args.dynamic_base, rng)
    if args.family == "fixed_loop":
        counts = [amplification(bounds)] * args.rows

    if args.family == "branch_loop":
        flags = correlate_flags(counts, args.branch_selectivity, args.correlation, rng)
    else:
        flags = exact_flags(args.rows, args.branch_selectivity, rng)

    retained = exact_flags(args.rows, args.filter_selectivity, rng)
    rows = []
    for index in range(args.rows):
        if args.data_access_pattern == "same_key_repeated":
            lookup_key = 0
        else:
            lookup_key = index % max(1, args.lookup_keys)
        rows.append(
            {
                "id": index,
                "value": rng.randint(1, 1_000_000),
                "flag": flags[index],
                "loop_count": counts[index],
                "retained": retained[index],
                "lookup_key": lookup_key,
            }
        )

    summary = summarize(rows, bounds)
    summary.update(
        {
            "schema_version": "1.0.0",
            "family": args.family,
            "seed": args.seed,
            "requested_distribution": args.distribution,
            "requested_branch_selectivity": args.branch_selectivity,
            "requested_filter_selectivity": args.filter_selectivity,
            "branch_loop_correlation": args.correlation,
            "data_access_pattern": args.data_access_pattern,
            "udf_chain_length": args.udf_chain_length,
            "opaque_component": "deterministic_cpu_kernel" if args.family == "opaque_call" else None,
            "filter_selectivity_realized": sum(retained) / args.rows if args.rows else None,
            "expected_work_before_filter_units": args.rows * amplification(bounds),
            "expected_work_after_filter_units": sum(retained) * amplification(bounds),
        }
    )
    return rows, summary


def write_dataset(rows: list[dict[str, int]], summary: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = ["id", "value", "flag", "loop_count", "retained", "lookup_key"]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    summary["dataset_path"] = str(output)
    summary["dataset_sha256"] = digest
    metadata_path = output.with_suffix(output.suffix + ".metadata.json")
    metadata_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--family", choices=FAMILIES, default="fixed_loop")
    result.add_argument("--rows", type=int, default=100)
    result.add_argument("--seed", type=int, default=42)
    result.add_argument("--loop-bounds", type=int, nargs="*", default=[8, 10, 12])
    result.add_argument("--distribution", choices=("constant", "uniform", "skewed", "bimodal"), default="constant")
    result.add_argument("--dynamic-base", type=int, default=10)
    result.add_argument("--branch-selectivity", type=float, default=0.5)
    result.add_argument("--filter-selectivity", type=float, default=0.5)
    result.add_argument("--correlation", choices=("independent", "positive", "negative"), default="independent")
    result.add_argument(
        "--data-access-pattern",
        choices=("none", "one_lookup", "repeated_lookup", "same_key_repeated", "varying_key_repeated"),
        default="none",
    )
    result.add_argument("--lookup-keys", type=int, default=16)
    result.add_argument("--udf-chain-length", type=int, choices=(1, 2, 3), default=1)
    result.add_argument("--output", type=Path, required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    for probability in (args.branch_selectivity, args.filter_selectivity):
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise SystemExit("selectivities must be in [0, 1]")
    rows, summary = generate(args)
    write_dataset(rows, summary, args.output.resolve())
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
