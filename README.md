# Representation adequacy and decision reach

## Overview

This archive contains the code, frozen inputs, raw measurements, normalized
results, query plans, protocols, and environment records needed to inspect and
reproduce the experiments. It contains no submission source, development
notes, conversation transcripts, local virtual environments, or
author-identifying paths.

## Hardware and software requirements

The portable validation path needs Python 3.10 or newer, GNU Make, and
`sha256sum`. The native PostgreSQL paths need Docker, or Ubuntu 22.04 with
PostgreSQL 14.24, PL/Python, the PostgreSQL server headers, GCC, Python 3.10,
`psycopg2`, and Matplotlib. The released-workload paths additionally require
the pinned third-party inputs listed in `THIRD_PARTY.md`.

The packaged archive is about 25 MB unpacked. Native studies create private
clusters and generated data below `/tmp`; reserve at least 5 GB of free disk
and 8 GB RAM. Q10 also needs the 249 MB SF1 source database. These are safe
minimums, not measured resource peaks.

## Quick smoke test and artifact verification

Requirements: Python 3.10 or newer, GNU Make, and `sha256sum`.

```bash
make verify
```

This checks archive integrity, required evidence, semantic gates, headline
statistics, corpus size, and package hygiene. It does not rerun timed database
experiments.

For a small executable check using only Python and SQLite:

```bash
make smoke
```

On the reference environment, verification, unit tests, and the three-trial
SQLite smoke test each complete in well under a minute. Timed native studies
take minutes to tens of minutes depending on image availability and hardware;
third-party setup and full multi-system controlled reruns can take longer.

## Repository structure

- `src/`, `systems/`, `configs/`, `workloads/`: controlled E1--E4 framework,
  adapters, generated inputs, and workload definitions.
- `scripts/run/`, `scripts/analyze/`: experiment runners and normalizers.
- `results/`, `raw/`, `plans/`, `metadata/`: frozen controlled, UDFBench,
  PostgreSQL inlining/COST, PRISM, GRACEFUL, and artifact-boundary evidence.
- `studies/e3_mapping_sensitivity/`: 36-cell native mapping sweep, source,
  raw trials, normalized results, and plans.
- `studies/postgresql_cost_grid/`: 54-cell scalar-COST decision audit.
- `studies/duckdb_overhead/`: fixed-interface callback diagnostic.
- `studies/join_consequence/`: preregistration, data generator, support
  extension, raw trials, plans, estimator-error sweep, and regime map.
- `studies/natural_q10/`: prospectively fixed PRISM/TPC-H Q10 matched
  mechanisms, sampled estimator, negative cells, and R4a eligibility diagnostic.
- `studies/e5_paired_ratio/`: randomized 20-block paired confirmation.
- `studies/corpus/`: final 35-work coding, candidate audit, exclusions, and
  evidence coverage.

## Environment, data setup, and build

`ENVIRONMENT.md` records the measured platform and toolchain. Native Docker
entry points build their pinned Ubuntu/PostgreSQL environment automatically.
For released workloads, obtain the exact source artifact identified in
`THIRD_PARTY.md` and verify its commit or SHA-256 before executing a study.
The Q10 input has a complete source-build and deterministic-generation path
below; no privately hosted input is required.

## Headline reproduction

Run reproduction commands from the artifact root. All native entry points
default to fresh output directories under `/tmp` and leave the packaged
frozen measurements unchanged.

| Paper evidence | Reproduction or verification path | Default/frozen output |
|---|---|---|
| Controlled E3 | `scripts/run/run_e3.py` followed by `scripts/analyze/analyze_e3.py` using the frozen manifest and preregistration | `results/{raw,normalized,plans}/e3-20260830T051639Z/` |
| Controlled E4 | `scripts/run/run_e4.py` followed by `scripts/analyze/analyze_e4.py` using the frozen manifest and preregistration | `results/{raw,normalized,plans}/e4-20260830T044613Z/` |
| Figure 1 / native E3 | `studies/e3_mapping_sensitivity/code/run_e3_mapping_sensitivity.sh` | `/tmp/e3-mapping-output` |
| Table 7 / held-out join study, calibration, controls, reversal, and scale check | `studies/join_consequence/code/run_e5_decision_consequence.sh` | `/tmp/join-consequence-output` |
| Randomized 20-block confirmation | `studies/e5_paired_ratio/code/run.sh` | paths documented in that script and protocol |
| Prepare and verify Q10 inputs | `studies/natural_q10/code/prepare_q10_inputs.sh /tmp/q10-inputs` | `/tmp/q10-inputs/tpch_sf1.duckdb` and its pinned DuckDB build |
| Q10 R1/R2 | set `Q10_SOURCE_DB` and `DUCKDB_CLI`, then run `studies/natural_q10/code/run_portable.sh` | caller-selected portable output directory |
| Q10 R4a diagnostic | set the same inputs, then run `studies/natural_q10/code/run_r4a_repair_portable.sh` | caller-selected portable output directory |
| UDFBench validation | `scripts/run/run_task2_udfbench.py` and `scripts/run/task11_udfbench_scale.py` after the pinned setup in `THIRD_PARTY.md` | frozen UDFBench files under `raw/` and `results/` |

Controlled E3/E4 are multi-system campaigns rather than single portable shell
commands. Their generators, run-specific manifests, preregistrations, runner
arguments, raw trials, plans, and analysis scripts are all retained. Execute
them only in a fresh extraction or with a new run ID: the scripts deliberately
refuse to overwrite existing results.

## Full reproduction

### Controlled framework and E1--E2 portable check

```bash
make test
make controlled-sqlite
make analyze-controlled
```

The Make targets above are a lightweight framework check. They do not claim to
rerun the frozen multi-system E3/E4 campaigns; use the headline paths above for
those campaigns.

DuckDB runs require the version recorded in
`configs/systems/duckdb-requirements.txt`. PostgreSQL runs require version
14.24 and PL/Python as recorded in `metadata/environment.json`.

### Native PostgreSQL studies

The code and generated inputs are under `studies/`. Run inside Ubuntu 22.04
with PostgreSQL 14.24, `postgresql-server-dev-14`, PL/Python, GCC, Python 3.10,
`psycopg2`, and Matplotlib. Every study creates a private cluster under `/tmp`,
uses a non-default port, disables JIT/parallel gather where specified, and
removes the temporary cluster after completion.

The two self-contained Docker entry points write into fresh `/tmp` output
directories by default and never overwrite the frozen evidence:

```bash
studies/e3_mapping_sensitivity/code/run_e3_mapping_sensitivity.sh
studies/join_consequence/code/run_e5_decision_consequence.sh
```

The join entry point intentionally executes the complete frozen three-phase
campaign (calibration, confirmation, and held-out scale); it has no selective
phase mode.

For the PostgreSQL COST grid, set `STUDY_OUTPUT_DIR` to an empty directory and
execute `studies/postgresql_cost_grid/code/run_experiment.py` in the recorded
PostgreSQL/Python environment.

### Natural Q10 input acquisition

The following command clones the public PRISM and modified DuckDB sources at
their full pinned commits, builds the recorded DuckDB fork, generates TPC-H
SF1 with deterministic DBGen defaults, and verifies byte-level SHA-256 hashes
for all four projections consumed by the experiment:

```bash
studies/natural_q10/code/prepare_q10_inputs.sh /tmp/q10-inputs
```

This source build requires network access, Git, GNU Make, Python 3 with
`venv`, GCC/G++ with C++20 support, and about 5 GB of free disk space. TPC
DBGen is obtained from the pinned public source; use of that generator is
subject to its bundled license. Reviewers who already built the pinned CLI may
skip the clone/build while retaining the same data checks:

```bash
DUCKDB_CLI=/path/to/pinned/duckdb \
  studies/natural_q10/code/prepare_q10_inputs.sh /tmp/q10-inputs
```

Then run Q10 with the two paths printed by the preparation script:

```bash
Q10_SOURCE_DB=/tmp/q10-inputs/tpch_sf1.duckdb \
DUCKDB_CLI=/tmp/q10-inputs/source/prism/build/release/duckdb \
STUDY_OUTPUT_DIR=/tmp/q10-r1-r2 \
  studies/natural_q10/code/run_portable.sh
```

The historical source database SHA-256 is
`f04b8e41e3e1c0829e96b02cbb6feebd9fff39959a141dec4dc1074500972191`.
A clean regeneration can have a different physical database hash because the
historical file retained catalog objects created during the PRISM audit. The
experiment reads only four projections. The preparation script requires all
four regenerated CSVs to match the frozen byte-level hashes and sizes in
`studies/natural_q10/protocol/environment.json`; this equivalence was validated
against the historical source.

The large Q10 SF1 source database is therefore generated rather than
redistributed. The files without the `_portable` suffix are retained
byte-for-byte so the prospective hash manifests remain independently
checkable. UDFBench/PRISM/GRACEFUL inputs must be retrieved from the canonical
sources in `THIRD_PARTY.md`.

## Expected outputs

- Natural Q10 R1/R2: different plan signatures, identical 6,001,215 calls,
  runtime-ratio interval crossing 1.
- Natural Q10 R4a diagnostic: exact semantic hash, zero boundary calls,
  paired ratio 1.985 with 95% CI [1.922, 2.037].
- Randomized join follow-up: paired ratios 11.98 and 10.46; both confidence
  intervals exclude 1.
- Final corpus: 35 retained works.

All failed, unsupported, high-variance, and semantically invalid cases remain
in the frozen evidence rather than being silently removed.

## Known portability limitations and third-party boundaries

- PostgreSQL timing is hardware- and session-sensitive. Reproduction should
  first compare semantics, plans, and exact call counts; timing need not be
  byte-identical to the frozen measurements.
- Q10 requires a locally generated SF1 database and pinned DuckDB executable.
  `prepare_q10_inputs.sh` builds both from public pinned sources and verifies
  every experiment-consumed projection before the portable runs.
- UDFBench, PRISM, and GRACEFUL remain subject to their upstream code, data,
  and environment boundaries. `THIRD_PARTY.md` records what is included,
  executed, partially executed, or artifact-blocked.
- No cross-DBMS absolute-runtime comparison is intended.
