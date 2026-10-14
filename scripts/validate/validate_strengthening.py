#!/usr/bin/env python3
"""Quick, dependency-free verification of the strengthening evidence."""

import csv
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_json(relative):
    with (ROOT / relative).open(encoding="utf-8") as handle:
        return json.load(handle)


def close(actual, expected, tolerance=1e-6):
    if not math.isclose(actual, expected, rel_tol=tolerance, abs_tol=tolerance):
        raise AssertionError(f"expected {expected}, found {actual}")


q10 = load_json("results/natural_q10/analysis.json")
primary = q10["primary"]
assert primary["verdict"] == "PARTIAL"
assert primary["semantic_identity"] is True
assert primary["plans_differ"] is True
close(primary["call_ratio"], 1.0)
assert primary["runtime_ratio_ci_low"] < 1 < primary["runtime_ratio_ci_high"]

summary = {row["cell"]: row for row in q10["summaries"]}
assert summary["R1_K1"]["udf_calls"] == 6_001_215
assert summary["R2_K1"]["udf_calls"] == 6_001_215
assert summary["R1_K1"]["plan_signature"] != summary["R2_K1"]["plan_signature"]

r4a = load_json("results/natural_q10/r4a_repair/analysis.json")
assert r4a["r4a_inlined"] is True
assert r4a["gate_calls"] == {"R0": 6_001_215, "R4A": 0}
close(r4a["paired_ratio"]["ratio"], 1.9849953481278897)
assert r4a["paired_ratio"]["ci_low"] > 1

paired = load_json("results/e5_paired_ratio/paired_summary.json")
assert len(paired) == 2 and all(row["semantic_equal"] for row in paired)
close(paired[0]["paired_ratio"]["ratio"], 11.982176632224473)
close(paired[1]["paired_ratio"]["ratio"], 10.464464998546678)
assert all(row["paired_ratio"]["ci_low"] > 1 for row in paired)

original = load_json("results/e5_paired_ratio/original_direct_ratio.json")
close(original["confirmation"]["ratio"], 8.70917)
assert original["confirmation"]["ci_low"] > 1

with (ROOT / "results/survey_flow/corpus_flow.csv").open(newline="", encoding="utf-8") as handle:
    flow = list(csv.DictReader(handle))
assert flow[-1]["stage"] == "final_corpus" and int(flow[-1]["retained"]) == 35

print("strengthening evidence: OK")
print("Q10 R1/R2: plan change, identical 6,001,215 calls, ratio CI crosses 1")
print("Q10 R4a: inlined, exact semantics, paired ratio 1.985 [1.922, 2.037]")
print("E5 randomized follow-up: ratios 11.98 and 10.46; both CIs exclude 1")
print("Corpus flow: 35 retained works")
