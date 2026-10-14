"""Seeded data generation for E5-DC.

Frozen by PREREGISTRATION.md section 4. The generator is deterministic given a
seed, so the calibration dataset (seed 20260923) and the held-out confirmation
dataset (seed 41) are independent draws from the same declared distribution.
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from pathlib import Path

# True procedural work per call, by segment. These are the physical truth that
# M2 reports and that M1 collapses into a single global mean.
WORK_CHEAP = 10
WORK_COSTLY = 1000

SEGMENTS = ("CHEAP", "COSTLY")
TIERS = ("GOLD", "SILVER")


@dataclass(frozen=True)
class DataSpec:
    """Declared shape of one generated dataset."""

    seed: int
    fact_rows: int
    customers: int
    gold_fraction: float
    regions: int
    costly_fraction: float = 0.5

    @property
    def gold_customers(self) -> int:
        return max(1, int(round(self.customers * self.gold_fraction)))


def global_work_units(spec: DataSpec) -> int:
    """Row-weighted mean work, i.e. the best single number M1 could report."""
    costly = spec.costly_fraction
    return int(round(costly * WORK_COSTLY + (1.0 - costly) * WORK_CHEAP))


def generate(spec: DataSpec, out_dir: Path) -> dict[str, Path]:
    """Write fact, customer and region CSVs; return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(spec.seed)

    customer_path = out_dir / f"e5_customer_seed{spec.seed}.csv"
    gold_ids = set(rng.sample(range(spec.customers), spec.gold_customers))
    with customer_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["cust_id", "tier"])
        for cust_id in range(spec.customers):
            writer.writerow([cust_id, "GOLD" if cust_id in gold_ids else "SILVER"])

    region_path = out_dir / f"e5_region_seed{spec.seed}.csv"
    with region_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["region_id", "region_name"])
        for region_id in range(spec.regions):
            writer.writerow([region_id, f"REGION_{region_id:02d}"])

    fact_path = out_dir / f"e5_fact_seed{spec.seed}.csv"
    with fact_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "cust_id", "segment", "value", "loop_count"])
        for row_id in range(spec.fact_rows):
            segment = "COSTLY" if rng.random() < spec.costly_fraction else "CHEAP"
            loop_count = WORK_COSTLY if segment == "COSTLY" else WORK_CHEAP
            writer.writerow([
                row_id,
                rng.randrange(spec.customers),
                segment,
                rng.randrange(1, 1_000_000),
                loop_count,
            ])

    return {"fact": fact_path, "customer": customer_path, "region": region_path}
