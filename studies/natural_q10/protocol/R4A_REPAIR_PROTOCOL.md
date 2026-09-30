# Post-primary R4a eligibility repair protocol

Frozen after the primary Task 11 run and before this diagnostic was executed.
The primary R1/R2 result is immutable. Its intended R4a arm retained the SQL
function boundary because PostgreSQL could not prove that the declared `STRICT`
function body was strict, so that arm did not instantiate R4a and is excluded
from the matched-mechanism comparison.

This diagnostic repeats only R0 and R4a. R0 is unchanged. R4a removes the
`STRICT` declaration from the simple SQL predicate; this is equivalent on the
TPC-H Q10 columns, which contain no NULLs, and semantic equality remains a hard
gate. Success requires that `q10_probe` disappear from the natural plan.

- Same frozen SF1 source, projection, schema, indexes, query, and PostgreSQL.
- Three warmup and ten measured randomized complete blocks (seed 20260928).
- Exact R0 calls; R4a boundary calls are zero by construction and plan
  inspection must establish inlining.
- Direct paired bootstrap interval for median(R0)/median(R4a), 10,000 resamples.
- All results retained; this is a transparent post-primary mechanism diagnostic,
  not a preregistered replacement for the partial R1/R2 result.

