# Task 11 preregistration — UDFBench scale robustness

Status: `FROZEN_BEFORE_DATA_DOWNLOAD_AND_PRIMARY_TIMING`

## Research question

Do the representation-related phenomena observed in the frozen tiny-data
UDFBench validation remain qualitatively present when the same released
workloads execute on the official UDFBench `small` dataset?

## Frozen inputs

- UDFBench repository commit:
  `8da987566590e2534f24cd97bcfaa9157c24962c`
- Dataset DOI/version: `10.5281/zenodo.14260428`, version 1.0.0
- Published archive MD5: `1f97ebe9d5041fa6d031c95e9691c3ed`
- Before-manifest SHA-256:
  `55ce9059ed4a2edcfb00cc9adb6b114deb0d65fde874eaf6fae9543ddaa57215`
- Baseline: frozen Task 2 `tiny` semantic, plan, diagnostic, and timing outputs;
  no Task 2 file will be regenerated.
- Larger scale: official `small` CSVs extracted from the published archive.

## Selected workloads and systems

- Q8: SQLite, DuckDB, PostgreSQL.
- Q14: SQLite, DuckDB, PostgreSQL.
- Q17: DuckDB only for primary execution. SQLite remains
  `ENGINE_UNSUPPORTED`; PostgreSQL remains `DIALECT_FAILURE` based on the
  released ports and will not be repaired.

No other query or DBMS will be added.

## Hypotheses

- **H1:** procedural work/UDF exposure should remain present at larger scale.
- **H2:** workloads with relational filtering before UDF execution should
  continue to show reduced UDF-reaching tuples or work where measurable.
- **H3:** larger scale may amplify runtime differences, but no monotonic or
  linear scaling law is assumed.
- **H4:** semantic or resource failures at larger scale are first-class results.
- **H5:** results are within-system only; no cross-engine ranking is permitted.

These hypotheses may fail.

## Semantic gate

For each selected workload/system at `small`, execute the unmodified released
query/UDF port, canonicalize and hash its complete result, and record row count,
NULL behavior, exception, and dialect status. Q8 and Q14 enter primary timing
only if their outputs agree across at least two passing released ports. Q17 may
enter within-DuckDB timing if repeated execution produces the same canonical
hash. Invalid outputs, dialect failures, unsupported adapters, timeouts, and
resource failures are retained and excluded from primary performance analysis.

## Representation measurements

- All: input/output cardinalities, natural plan, estimated cardinalities where
  exposed, runtime, and planning time where meaningful.
- Q8: join rows plus summed citation-target and author-list JSON element counts
  from separate diagnostic queries that do not alter the primary UDF bodies.
- Q14: relevant base-relation sizes and qualifying relational rows before
  result projection, plus filter/join placement from natural plans.
- Q17: non-null input abstracts and emitted term/document row count.

Diagnostics are not claimed to be optimizer-visible.

## Timing protocol

- Q8/Q14 on every semantically passing system: 3 warmups and 10 measured
  trials; 600-second per-trial timeout.
- Q17 on DuckDB: 1 warmup and 5 measured trials; 900-second per-trial timeout.
  The reduction is frozen because Task 2's tiny median is 33.6 seconds and the
  official small input has about 7.6 times as many abstracts.
- DuckDB uses one thread; PostgreSQL disables JIT and parallel gather; SQLite
  is single-threaded, matching Task 2.
- Plans and semantic-validation executions are outside timed trials.
- Report median, IQR, CV, minimum, and maximum. CV above 0.10 is flagged and
  retained.
- No timeout will be increased after observing a result.

## Interpretation limits

- Compare scales only within a system/workload.
- Do not infer DBMS rankings or production scalability.
- Do not upgrade E3 beyond partial support or E4 beyond not testable without
  the required experimental contrasts.
- The decision is `STRONGER_EXTERNAL_VALIDITY` only if at least two selected
  workloads pass semantic validation at official small scale and retain their
  qualitative representation phenomena.
