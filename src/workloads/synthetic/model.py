"""Pure deterministic kernels shared by generators and system adapters."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence


def amplification(bounds: Iterable[int]) -> int:
    result = 1
    for bound in bounds:
        if bound < 0:
            raise ValueError("loop bounds must be non-negative")
        result *= bound
    return result


def fixed_loop_checksum(value: int, bounds: tuple[int, ...]) -> int:
    """Execute product(bounds) deterministic state transitions."""
    checksum, _observed_iterations = fixed_loop_checksum_observed(value, bounds)
    return checksum


def fixed_loop_checksum_observed(value: int, bounds: tuple[int, ...]) -> tuple[int, int]:
    """Return the checksum and a counter incremented at every leaf iteration.

    The counter is local to one UDF invocation. Updating it once per leaf adds
    constant instrumentation work to every procedural work unit.
    """
    state = int(value) & 0x7FFFFFFF
    observed_iterations = 0

    def visit(depth: int) -> None:
        nonlocal state, observed_iterations
        if depth == len(bounds):
            state = (state * 1_103_515_245 + 12_345) & 0x7FFFFFFF
            observed_iterations += 1
            return
        for index in range(bounds[depth]):
            state = (state + index + depth) & 0x7FFFFFFF
            visit(depth + 1)

    visit(0)
    return state, observed_iterations


def dynamic_loop_checksum(value: int, iterations: int) -> int:
    if iterations < 0:
        raise ValueError("iterations must be non-negative")
    state = int(value) & 0x7FFFFFFF
    for index in range(iterations):
        state = (state * 1_103_515_245 + 12_345 + index) & 0x7FFFFFFF
    return state


def branch_checksum(value: int, flag: int, iterations: int) -> int:
    """Execute expensive work only on the selected deterministic branch."""
    return dynamic_loop_checksum(value, iterations) if flag else int(value)


def data_access_checksum(
    value: int,
    iterations: int,
    lookup_key: int,
    pattern: str,
    lookup: Mapping[int, int],
) -> int:
    """Controlled in-process surrogate for visible lookup access patterns."""
    state = int(value)
    if pattern == "none":
        return state
    if pattern == "one_lookup":
        return dynamic_loop_checksum(state + lookup[lookup_key], 1)
    if pattern not in {"repeated_lookup", "same_key_repeated", "varying_key_repeated"}:
        raise ValueError(f"unsupported data-access pattern: {pattern}")
    keys = sorted(lookup)
    for index in range(iterations):
        key = keys[(lookup_key + index) % len(keys)] if pattern == "varying_key_repeated" else lookup_key
        state = dynamic_loop_checksum(state + lookup[key], 1)
    return state


def udf_chain_checksum(value: int, stage_bounds: Sequence[tuple[int, ...]]) -> int:
    if len(stage_bounds) not in (2, 3):
        raise ValueError("controlled UDF chains must contain two or three stages")
    state = int(value)
    for bounds in stage_bounds:
        state = fixed_loop_checksum(state, bounds)
    return state


def opaque_cpu_surrogate(value: int, work_units: int) -> int:
    """Deterministic CPU-only surrogate; never performs external I/O or sleep."""
    return dynamic_loop_checksum(value ^ 0x5A5A5A5A, work_units)
