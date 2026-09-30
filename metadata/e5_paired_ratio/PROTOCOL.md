# E5 paired-ratio validation protocol

Frozen 2026-09-28 before this follow-up was executed. This validation does not
replace or relabel the calibration-selected E5 study. It repeats only its fixed
held-out primary cell and fixed 2x-scale cell to obtain a direct paired effect
interval under genuinely randomized within-block arm order.

- Data specifications: unchanged seed-41 confirmation and held-out 2x scale.
- Query: COSTLY segment, K=400, unchanged M1/M2 definitions.
- Arms: M1 collapsed and M2 conditioned; no hints or disabled paths.
- Semantic gate and exact call counter required before timing.
- Three complete warmup blocks and 20 measured complete blocks per scale.
- M1/M2 order independently shuffled in every block with seed 20260928.
- Primary statistic: median(M1)/median(M2).
- Uncertainty: paired block bootstrap, 10,000 resamples, seed 20260928.
- Report median, IQR, CV, call counts, result equality, and ratio interval.
- No observation may be dropped; both scales are retained regardless of result.

