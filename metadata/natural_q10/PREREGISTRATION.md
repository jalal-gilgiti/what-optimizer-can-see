# Prospective protocol: PRISM/TPC-H Q10 representation-to-decision study

Status: **FROZEN BEFORE EXECUTION**

Frozen on 2026-09-28 before any Task 11 plan or timing was collected. Existing
PRISM artifact inspection and the published query source motivated the workload;
no Task 11 result was available when this protocol was written.

## Research question

On the released PRISM UDF-bearing TPC-H Q10 logical workload and unmodified
TPC-H SF1 data distribution, do progressively richer optimizer-visible
representations change a natural plan, UDF exposure, and execution time?

## Frozen workload and data

- Source: `prism/samples/tpch_queries.sql`, Query 10, and
  `prism/samples/tpch_udfs.sql`, `q10conditions`.
- Database: the previously frozen PRISM TPC-H SF1 DuckDB database. Its complete
  file hash is recorded before export.
- Tables/columns are a lossless projection of Q10's required columns; rows and
  values are not sampled, rescaled, or redistributed.
- The query text is fixed across all experimental arms. The released integer
  predicate is normalized to an equivalent Boolean function so PostgreSQL's
  documented planner-support selectivity interface can be evaluated. This is
  the only syntactic adaptation. `discount_price` is an invariant SQL function
  in every arm.
- No hint, forced join order, disabled path type, or per-arm data/statistics/
  index change is allowed.

## Representations

- **R0 OPAQUE:** instrumented native predicate; PostgreSQL defaults (`COST 100`)
  and default Boolean-function selectivity.
- **R1 SCALAR:** the same predicate with one sampled global per-call `COST`;
  selectivity remains PostgreSQL's default.
- **R2 SAMPLED:** the same predicate plus sampled per-call cost and sampled
  workload selectivity through `SupportRequestCost` and
  `SupportRequestSelectivity`.
- **R4a EXPOSED:** an equivalent eligible `LANGUAGE SQL` definition; the query
  call remains unchanged and PostgreSQL may inline the relational predicate.

R0/R1/R2 use the same C implementation and exact invocation counter. R4a is a
semantic matched arm and should have zero remaining boundary calls if inlined.

## Practical estimator and acquisition cost

A deterministic 10,000-row `TABLESAMPLE ... REPEATABLE(20260928)` sample of the
`ORDERS`--`LINEITEM` join is materialized once. The estimator executes the UDF
on this sample to estimate selectivity and profiles it against the equivalent
native predicate to estimate incremental per-call cost. The wall time of sample
construction and all profiling queries is the acquisition cost. The sample is
not used to change the query, data, indexes, or success thresholds.

## Mapping grid

The sampled per-call cost is multiplied by
`K_B in {0.25, 0.5, 1, 2, 4}`. The primary cell is `K_B=1`. All grid cells are
retained whether or not they change a plan.

## Endpoints and protocol

1. Capture natural `EXPLAIN (FORMAT JSON, VERBOSE, COSTS)` plans.
2. Execute each arm and require identical canonical result hashes before timing.
3. Record exact boundary-UDF calls for R0/R1/R2 and verify R4a inlining from the
   plan plus a zero boundary-call counter.
4. Perform three untimed warmups per cell.
5. Run ten randomized complete timing blocks (fixed seed 20260928), one
   observation per cell per block. DDL and counter reads are outside the timed
   interval; parse/plan/execute/fetch are inside it.
6. Report median, IQR, CV, and seeded 10,000-resample bootstrap intervals.
   Runtime contrasts use a paired block bootstrap of the ratio of medians.
7. Report plan-capture time, support-call time, estimator acquisition time, and
   acquisition amortization separately.

## Primary comparison and success criteria

The primary comparison is R1 versus R2 at `K_B=1`. It is `SUPPORTED` only if:

1. all arms return byte-identical canonical results;
2. R1 and R2 select a different natural join order or predicate-placement plan;
3. their exact boundary-UDF calls differ by at least 2x;
4. the paired median runtime ratio is at least 2x and its 95% interval excludes 1;
5. CV is at most 0.10 in both primary cells; and
6. the R2 benefit exceeds estimator acquisition cost within ten executions.

Criteria 1--2 failing yields `NOT SUPPORTED`; partial satisfaction yields
`PARTIAL`. R4a is a matched mechanism comparison, not a substitute for the
primary R1/R2 criterion.

## Negative results and stopping rule

Every fixed cell, plan, semantic result, and measured trial is retained. No
workload, scale, index, K value, success threshold, or representation may be
changed after seeing Task 11 plans or timings. If the fixed study is null, that
null result is the result; another benchmark workload will not be substituted.

