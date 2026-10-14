#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import json
import os
import random
import statistics
from pathlib import Path

ARTIFACT_ROOT = Path(__file__).resolve().parents[3]
SOURCE = Path(os.environ.get(
    "JOIN_STUDY_CODE", ARTIFACT_ROOT / "studies/join_consequence/code"
)).resolve()
OUT = Path(os.environ.get(
    "STUDY_OUTPUT_DIR", ARTIFACT_ROOT / "studies/e5_paired_ratio"
)).resolve()


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


import sys
sys.path.insert(0, str(SOURCE))
rx = load("e5_rx", SOURCE / "run_experiment.py")
st = load("e5_stats", SOURCE / "stats.py")

RAW = OUT / "raw"
NORMALIZED = OUT / "normalized"
LOGS = OUT / "logs"
SEED = 20260928
WARMUPS = 3
BLOCKS = 20
K = 400


def quantile(values, p):
    ordered = sorted(values)
    x = p * (len(ordered) - 1)
    lo = int(x)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] * (1 - (x - lo)) + ordered[hi] * (x - lo)


def paired_ratio(m1, m2):
    rng = random.Random(SEED)
    values = []
    for _ in range(10_000):
        idx = [rng.randrange(len(m1)) for _ in m1]
        values.append(statistics.median(m1[i] for i in idx) /
                      statistics.median(m2[i] for i in idx))
    return {"ratio": statistics.median(m1) / statistics.median(m2),
            "ci_low": quantile(values, .025), "ci_high": quantile(values, .975)}


def run_scale(name, spec):
    rx.RAW = RAW
    rx.LOGS = LOGS
    paths = rx.datagen.generate(spec, RAW / "data")
    cluster, socket, process, log_handle = rx.start_cluster("paired-" + name)
    rows = []
    try:
        conn = rx.connect(socket)
        rx.setup_database(conn, spec, paths)
        semantic = {}
        for rep in ("M1", "M2"):
            outcome = rx.execute_once(conn, "COSTLY", rep, K, spec)
            semantic[rep] = outcome
        if semantic["M1"]["rows"] != semantic["M2"]["rows"]:
            raise RuntimeError("semantic mismatch")
        rng = random.Random(SEED + (0 if name == "confirmation" else 1))
        for block in range(WARMUPS + BLOCKS):
            order = ["M1", "M2"]
            rng.shuffle(order)
            for position, rep in enumerate(order):
                outcome = rx.execute_once(conn, "COSTLY", rep, K, spec)
                rows.append({"scale": name, "phase": "warmup" if block < WARMUPS else "measured",
                             "block": block, "position": position, "representation": rep,
                             "elapsed_ms": outcome["elapsed_ms"], "udf_calls": outcome["udf_calls"],
                             "udf_work_units": outcome["udf_work_units"]})
        conn.close()
    finally:
        rx.stop_cluster(cluster, socket, process, log_handle)
    measured = [r for r in rows if r["phase"] == "measured"]
    by = {rep: [r["elapsed_ms"] for r in sorted(measured, key=lambda x: x["block"])
                if r["representation"] == rep] for rep in ("M1", "M2")}
    return rows, {"scale": name, "semantic_equal": True,
                  "m1_calls": semantic["M1"]["udf_calls"], "m2_calls": semantic["M2"]["udf_calls"],
                  "m1": st.summarize(by["M1"], SEED), "m2": st.summarize(by["M2"], SEED),
                  "paired_ratio": paired_ratio(by["M1"], by["M2"])}


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def main():
    for p in (RAW, NORMALIZED, LOGS): p.mkdir(parents=True, exist_ok=True)
    all_rows, summaries = [], []
    for name, spec in (("confirmation", rx.CONFIRMATION_SPEC),
                       ("heldout_2x", rx.HELDOUT_SCALE_SPEC)):
        rows, summary = run_scale(name, spec)
        all_rows.extend(rows); summaries.append(summary)
    write_csv(RAW / "paired_trials.csv", all_rows)
    (NORMALIZED / "paired_summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__": main()
