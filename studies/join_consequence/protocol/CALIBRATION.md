# Calibration record — E5-DC

Per `PREREGISTRATION.md` section 7, calibration runs on the calibration seed family
(20260923) and is used **only** to choose magnitudes. Every calibration observation is
retained below, including the regimes that failed. Confirmation runs on held-out seed 41
with all parameters frozen at the values chosen here.

## Probe 1 — join method only (`probe.py`)

Swept `gold_fraction` in {0.02, 0.05, 0.10, 0.25, 0.50} x K in {100..1600} at
`costly_fraction=0.5`, 200k fact rows. Plans differed in join method at several cells, but
the fact access path was identical, so the UDF was evaluated on the same row set.

**Outcome: decision difference without consequence.** This is the failure mode the review
objected to, and it is reported, not discarded.

## Probe 2 — first real consequence (`probe2.py`)

Same mix, measured true UDF call counts and runtimes.

| gold | K | M1 calls | M2 calls | call ratio | M1 ms | M2 ms | ms ratio |
|---|---|---|---|---|---|---|---|
| 0.30 | 200-800 | 60,376 | 60,376 | 1.0x | ~266 | ~266 | 1.00x |
| 0.40 | 200-600 | 80,158 | 80,158 | 1.0x | ~365 | ~363 | ~1.00x |
| 0.40 | 800 | 139,995 | 80,158 | **1.75x** | 578 | 363 | **1.59x** |
| 0.50 | 200-300 | 149,773 | 149,773 | 1.0x | ~730 | ~727 | ~1.03x |

At gold=0.40, K=800 the mis-costing changed the fact access path (M1 bitmap-scanned every
COSTLY row; M2 index-scanned only the joined customers), producing genuine excess work.
**Below the preregistered 2x threshold**, so not sufficient.

Diagnosis: with an even segment mix the global mean M1 reports (505) is only ~2x from the
COSTLY truth (1000). The decision window in K is therefore only a factor of ~2 wide, and
the achievable work ratio is bounded by 1/gold_fraction at the tie point.

## Probe 3 — skewed segment mix (`probe3.py`)

A rare-but-expensive segment is both realistic and a far harder case for a global average:
at `costly_fraction=0.05` the row-weighted mean is **60 units against a COSTLY truth of
1000**, a ~17x understatement.

400k fact rows, 20k customers:

| gold | K | M1 calls | M2 calls | call ratio | M1 ms | M2 ms | ms ratio |
|---|---|---|---|---|---|---|---|
| 0.02 | 100 | 20,580 | 836 | **24.6x** | 87 | 11 | **7.84x** |
| 0.02 | 200 | 20,580 | 836 | **24.6x** | 85 | 9 | **9.20x** |
| 0.02 | 400 | 20,580 | 836 | **24.6x** | 85 | 9 | **9.30x** |
| 0.02 | 800 | 20,580 | 836 | **24.6x** | 89 | 14 | **6.61x** |
| 0.02 | 1600 | 20,580 | 20,580 | 1.0x | 104 | 88 | 1.17x |
| 0.05 | 100-1600 | 21,164 | 21,164 | 1.0x | ~90-113 | ~117-133 | ~1.0-1.5x |
| 0.10 | 100-1600 | 22,168 | 22,168 | 1.0x | ~105-128 | ~116-123 | ~1.0-1.1x |

The effect persists across four adjacent K values including the historical K=400, and
closes at K=1600 — a bounded decision region, not a knife edge.

## Parameters frozen for confirmation

| Parameter | Value | Why |
|---|---|---|
| `costly_fraction` | 0.05 | rare-but-expensive segment; global mean = 60 vs truth 1000 |
| `gold_fraction` | 0.02 | places the decision region across the historical K=400 |
| `fact_rows` | 800,000 | calibration reached only ~85 ms at 400k; doubled to clear the ~100 ms target |
| `customers` | 20,000 | unchanged |
| `regions` | 16 | unchanged |

Nothing about the SQL, schema, indexes, grid, measurement procedure, or success criteria
was changed during calibration. However, calibration selected `costly_fraction` and
`gold_fraction` after exploratory plans and timings, not only the dataset scale. That is a
deviation from sections 7 and 11 of the frozen protocol. The paper therefore describes the
result conservatively as **calibration-selected with held-out-seed confirmation**, not as a
fully preregistered experiment. This note records the deviation without altering the
original hashed preregistration.
