# Preregistration — E5-DC: Decision Consequence via Join Method Under Collapsed Procedural Cost

**Status when frozen:** written and hashed BEFORE any measurement run.
**Experiment label:** E5-DC
**Target system:** PostgreSQL 14.24 (Ubuntu 14.24-0ubuntu0.22.04.1), same vendored build
and container as `reviewer_strengthening/01_e3_mapping_sensitivity_final`.

---

## 1. Motivation

The VLDB EA&B review returned Weak Accept with one named gap:

> "One experiment: a case where the collapsed representation causes the optimizer to pick
> a plan that is measurably and substantially slower, with tight confidence intervals,
> ideally through a join order or predicate placement rather than a scan choice."

Existing evidence in the paper is insufficient for this claim, and this experiment does
not reuse it:

- E3 mapping sensitivity shows an **access-path** flip; runtimes are sub-millisecond and
  6 of 36 cells exceed CV 0.10.
- The scalar-COST grid (D3) shows a join change driven by a **scalar** COST, not by
  query-conditioned procedural information.
- The inlining result (114 ms -> 0.23 ms) shows the benefit of inlining, which is already
  known, not the cost of representation inadequacy.

## 2. Hypothesis

**H1 (primary).** When the true per-call procedural cost of a UDF depends on the queried
segment, a collapsed representation (M1: one global cost for all segments) causes
PostgreSQL to select a *different natural join method/order* than a query-conditioned
representation (M2: correct per-segment cost), and the M1-selected plan evaluates the UDF
substantially more often, producing a substantially longer runtime.

**H0 (null).** M1 and M2 select the same join plan, or the selected plans do not differ in
UDF invocation count, or the runtime difference is not separated from noise.

H0 is a valid, reportable outcome. It will be reported as NOT SUPPORTED.

## 3. Mechanism under test

PostgreSQL applies a single-table restriction clause at the scan node, so *predicate
placement* for such a clause is not a cost-based decision and is **not** the lever here.
The lever is **join method and join order**:

- Under a nested-loop join, a qual on the inner relation is re-evaluated on each rescan,
  so UDF invocations scale with (outer rows x inner rows scanned).
- Under a hash join, the same qual is evaluated once per inner row.

If the per-call cost of the UDF is **understated**, the planner underestimates the cost of
the repeated-evaluation plan and may choose it. The excess procedural work is then real,
observable, and attributable to the representation, not to a hint or a forced path.

The collapsed representation understates exactly when the queried segment is the expensive
one: M1 reports the global average for every segment, so for the high-work segment it
reports less than the truth.

## 4. Schema (fixed before execution)

```sql
CREATE TABLE e5_fact (
    id           bigint  NOT NULL,
    cust_id      bigint  NOT NULL,
    segment      text    NOT NULL,   -- 'CHEAP' or 'COSTLY'
    value        bigint  NOT NULL,
    loop_count   bigint  NOT NULL    -- true procedural work per call
);

CREATE TABLE e5_customer (
    cust_id      bigint  NOT NULL,
    tier         text    NOT NULL,
    PRIMARY KEY (cust_id)
);

CREATE TABLE e5_region (
    region_id    bigint  NOT NULL,
    region_name  text    NOT NULL,
    PRIMARY KEY (region_id)
);
```

Three tables, so join **order** as well as method is in the search space.

## 5. Representations

Identical data, identical statistics, identical indexes, identical GUCs. The only
difference is the value returned by the `SupportRequestCost` handler:

| Mode | Reported work units per call |
|---|---|
| M1_GLOBAL | `W_GLOBAL` for every segment (the corpus-wide average) |
| M2_CONDITIONED | `W_CHEAP` when the query constant is `'CHEAP'`; `W_COSTLY` when `'COSTLY'` |

`per_tuple = reported_work_units / K`, where `K` is the work-to-cost mapping factor,
exactly as in the frozen native bridge.

Reported units are set equal to the **true** loop counts for M2, and to the
row-count-weighted mean of the true loop counts for M1. M1 is therefore not a strawman:
it is the best single global number available to a representation that cannot condition
on the query.

## 6. Query under test (fixed before execution)

A three-table join in which the UDF predicate is applied to the fact relation:

```sql
SELECT count(*)::bigint, sum(e5_work(f.value, f.loop_count))::numeric
FROM   e5_fact f
JOIN   e5_customer c ON c.cust_id = f.cust_id
JOIN   e5_region   r ON r.region_id = (f.id % <REGIONS>)
WHERE  f.segment = '<SEGMENT>'
AND    c.tier = '<TIER>'
AND    e5_work(f.value, f.loop_count) >= 0;
```

No hints. No `enable_*` flags disabled. Identical text for M1 and M2; only the session
GUC `e5dc.mode` differs.

## 7. Calibration vs confirmation (declared in advance)

To avoid tuning on the confirmatory result:

- **Calibration phase** runs on seed 20260923 and is used **only** to choose magnitudes so
  that runtimes exceed ~100 ms and CV <= 0.10. Calibration outputs are retained and
  reported, never discarded.
- **Confirmation phase** runs on a **held-out seed 41 dataset** regenerated from scratch,
  with every parameter frozen at the calibrated values. The confirmatory result is what
  the paper reports.

If calibration fails to reach >=100 ms at CV <= 0.10, that is reported as a limitation and
the experiment is declared NOT SUPPORTED rather than re-tuned.

## 8. Grid

- Mapping factor `K` in {100, 200, 400, 800, 1600} — 400 is the historical value; the
  neighbours test persistence across adjacent mapping values.
- Segment in {CHEAP, COSTLY}.
- Representation in {M1_GLOBAL, M2_CONDITIONED}.
- Data scale in {calibration scale, held-out scale} for the persistence check.

Primary cell: segment = COSTLY at K = 400, held-out seed.

## 9. Measurement protocol

1. **Plan capture** — `EXPLAIN (FORMAT JSON)` for every cell; record join method, join
   order, and the node applying the UDF qual.
2. **Semantic gate** — execute and hash the full result set. M1 and M2 must produce
   byte-identical output hashes for the same segment. A mismatch invalidates the cell and
   no timing is reported for it.
3. **Exact UDF accounting** — the UDF increments an in-extension counter for invocations
   and accumulates true work units. Counters are read per execution, not inferred from the
   plan.
4. **Timing** — randomized complete blocks; 3 warmups discarded, 10 retained trials per
   cell. `EXPLAIN` excluded from timed sections.
5. **Statistics** — median, IQR, CV, and bootstrap 95% CI of the median (10,000 resamples,
   fixed seed).

## 10. Success criteria (all must hold for SUPPORTED)

For the primary cell, on the held-out dataset:

1. M1 and M2 produce byte-identical results within each segment.
2. M1 and M2 select a **different** natural join method or join order.
3. UDF invocation counts differ by at least 2x.
4. Median runtime differs by at least 2x.
5. Bootstrap 95% CIs of the two medians do **not** overlap.
6. CV <= 0.10 in both compared cells.
7. The plan difference persists at >= 2 adjacent values of K and on the held-out scale.

Criteria met partially -> reported as PARTIAL with the exact criteria that failed.
Criteria 1-2 failing -> reported as NOT SUPPORTED.

## 11. Explicitly prohibited

- Changing the SQL, schema, data distribution, or indexes after seeing a plan or timing.
- Selecting K after seeing which K produces a difference.
- Dropping high-variance cells.
- Using hints, `enable_*` toggles, or forced paths.
- Claiming a speedup from any cell with CV > 0.10.
- Presenting this result as a substitute for, or merging it with, the E3 sensitivity or
  inlining results.

## 12. Stop rule

One experiment. If the confirmatory run yields H0, report NOT SUPPORTED, keep every
artifact, and stop. Do not search for a different query, workload, or system.
