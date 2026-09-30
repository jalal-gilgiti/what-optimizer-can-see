#!/usr/bin/env python3
"""Dependency-free integrity and hygiene checks for the release archive."""

from __future__ import annotations

import csv
import ast
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

REQUIRED = [
    "README.md",
    "LICENSE",
    "THIRD_PARTY.md",
    "results/natural_q10/analysis.json",
    "results/e5_paired_ratio/paired_summary.json",
    "studies/e3_mapping_sensitivity/results/analysis.json",
    "studies/join_consequence/results/e5dc_cell_summary.csv",
    "studies/natural_q10/raw/trials.csv",
    "studies/e5_paired_ratio/raw/paired_trials.csv",
    "studies/corpus/corpus_10_dimension_matrix.csv",
]

for relative in REQUIRED:
    if not (ROOT / relative).is_file():
        raise AssertionError(f"missing required file: {relative}")

for path in ROOT.rglob("*"):
    lower = path.name.lower()
    if path.is_dir() and lower in {"__pycache__", ".claude", ".git"}:
        raise AssertionError(f"forbidden directory: {path.relative_to(ROOT)}")
    if path.is_file() and (lower.endswith(".pyc") or "prompt" in lower):
        raise AssertionError(f"forbidden file: {path.relative_to(ROOT)}")

for path in ROOT.rglob("*.py"):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

forbidden_names = re.compile(
    r"(chatgpt|\bopenai\b|\bcodex\b|\bgemini\b|\bclaude\b|"
    r"syed jalal|young-koo|kyung hee|jalal\.hashmi|\bhashmi\b|/mnt/store2|/home/hashmi)",
    re.IGNORECASE,
)
text_suffixes = {".py", ".sh", ".md", ".txt", ".json", ".csv", ".yaml", ".yml", ".c"}
for path in ROOT.rglob("*"):
    if path.resolve() == Path(__file__).resolve():
        continue
    if not path.is_file() or path.suffix.lower() not in text_suffixes or path.stat().st_size > 10_000_000:
        continue
    content = path.read_text(encoding="utf-8", errors="replace")
    match = forbidden_names.search(content)
    if match:
        raise AssertionError(f"forbidden release text {match.group(0)!r} in {path.relative_to(ROOT)}")

frozen_hashes = {
    "studies/join_consequence/protocol/PREREGISTRATION.md": "de44bee29799550672c7c11905a41ff70aad20cdb584d9ca31a9820de8d3c0c6",
    "studies/natural_q10/protocol/PREREGISTRATION.md": "ccdad9ed77f4046d11fa9917873b84fabeaabd31a71b5ab8c8941abc48e76aac",
    "studies/natural_q10/protocol/AMENDMENT_01.md": "b722b163938da591a80bf36461b115867ec1ccfdc413e2da7d37787f511e8c54",
    "studies/natural_q10/protocol/AMENDMENT_02.md": "6e86b4ebce969c824fd11c74033527e8f59e9c669fb5d443be9c5987d49ea6e3",
    "studies/natural_q10/code/q10_support.c": "d76c786dc2db41d3b7c5c6663f76e150b589e2ea438daed704fc92503e15c35a",
    "studies/natural_q10/code/run_experiment.py": "402f5e70cbfe5fbfaeb8a43f776c268f9c92420f60f649200e56bf20d609d2ff",
    "studies/natural_q10/code/stats.py": "231a40eede2354234a7c29d8e78ee3be249e9fa730435a29fcde1114111a9d07",
    "studies/natural_q10/code/run.sh": "b6ee5ad49e1cc2dca529ee9b3fcb771f838d579a3c8350981a79f3c40ead5ef2",
    "studies/natural_q10/protocol/R4A_REPAIR_PROTOCOL.md": "883da80b76bed92b914d3b5fed03f8896eca327b1eb35f2557695e4810344745",
    "studies/natural_q10/code/run_r4a_repair.py": "f9874d262f1cb43d5a1f526254dcaeaf7952594f7a0c8b7b4309e97410e86232",
    "studies/natural_q10/code/run_r4a_repair.sh": "aad563cda8505028f5f982ca73ef307e1d7570e5883046dca8f76daeeefc4ad3",
    "studies/e5_paired_ratio/protocol/PROTOCOL.md": "26d4444e803591e55e2ed7bb65b85feb72376b13a06e3412e1bc2b085da86fe5",
    "studies/e5_paired_ratio/code/run_paired.py": "d3c6395e3066ba91890ca75a3192992d126fc789211330cf9ce115744e461c80",
    "studies/e5_paired_ratio/code/run.sh": "083b9c41d83f4ea612b5a397767d41eaa24dd17efb8a44439d777f39fad638cc",
}
for relative, expected in frozen_hashes.items():
    observed = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
    if observed != expected:
        raise AssertionError(f"frozen source hash mismatch: {relative}")

with (ROOT / "studies/corpus/corpus_10_dimension_matrix.csv").open(newline="", encoding="utf-8") as handle:
    corpus = list(csv.DictReader(handle))
assert len(corpus) == 35

with (ROOT / "studies/natural_q10/results/analysis.json").open(encoding="utf-8") as handle:
    q10 = json.load(handle)
assert q10["primary"]["semantic_identity"] is True
assert q10["primary"]["plans_differ"] is True
assert q10["primary"]["call_ratio"] == 1.0

print("release integrity and hygiene: OK")
print(f"required evidence: {len(REQUIRED)} files")
print(f"corpus rows: {len(corpus)}")
