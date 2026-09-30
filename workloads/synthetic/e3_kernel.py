"""Canonical E3 data-dependent loop kernel with process-local observation."""

from __future__ import annotations


def dynamic_loop_checksum_observed(value: int, iterations: int) -> tuple[int, int]:
    if iterations < 0:
        raise ValueError("iterations must be non-negative")
    state = int(value) & 0x7FFFFFFF
    observed = 0
    for index in range(iterations):
        state = (state * 1_103_515_245 + 12_345 + index) & 0x7FFFFFFF
        observed += 1
    return state, observed
