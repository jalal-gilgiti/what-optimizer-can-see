from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workloads.synthetic.generate import generate
from workloads.synthetic.model import (
    amplification,
    branch_checksum,
    data_access_checksum,
    fixed_loop_checksum,
    fixed_loop_checksum_observed,
    opaque_cpu_surrogate,
    udf_chain_checksum,
)
from systems.sqlite.adapter import SQLiteAdapter


class WorkloadTests(unittest.TestCase):
    def arguments(self, **overrides):
        values = {
            "rows": 20, "seed": 7, "loop_bounds": [2, 3], "distribution": "uniform",
            "dynamic_base": 5, "family": "branch_loop", "branch_selectivity": 0.5,
            "filter_selectivity": 0.25, "correlation": "positive", "data_access_pattern": "none",
            "lookup_keys": 4, "udf_chain_length": 1,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_amplification(self):
        self.assertEqual(amplification((8, 10, 12)), 960)

    def test_checksum_is_deterministic(self):
        self.assertEqual(fixed_loop_checksum(11, (2, 3)), fixed_loop_checksum(11, (2, 3)))

    def test_observed_iterations_equal_amplification(self):
        checksum, observed = fixed_loop_checksum_observed(11, (8, 10, 12))
        self.assertEqual(checksum, fixed_loop_checksum(11, (8, 10, 12)))
        self.assertEqual(observed, 960)

    def test_generation_is_deterministic(self):
        first, first_summary = generate(self.arguments())
        second, second_summary = generate(self.arguments())
        self.assertEqual(first, second)
        self.assertEqual(first_summary, second_summary)

    def test_positive_correlation_increases_conditional_mean(self):
        _, summary = generate(self.arguments())
        self.assertGreaterEqual(summary["e_i_given_branch_realized"], summary["e_i_realized"])

    def test_branch_skips_expensive_kernel(self):
        self.assertEqual(branch_checksum(9, 0, 100), 9)
        self.assertNotEqual(branch_checksum(9, 1, 100), 9)

    def test_data_access_patterns_are_deterministic(self):
        lookup = {0: 11, 1: 13, 2: 17}
        first = data_access_checksum(5, 3, 0, "varying_key_repeated", lookup)
        second = data_access_checksum(5, 3, 0, "varying_key_repeated", lookup)
        self.assertEqual(first, second)

    def test_udf_chain_and_opaque_surrogate(self):
        self.assertIsInstance(udf_chain_checksum(3, [(2,), (3,)]), int)
        self.assertEqual(opaque_cpu_surrogate(5, 7), opaque_cpu_surrogate(5, 7))

    def test_e2_forms_are_equivalent_with_expected_work(self):
        rows, _ = generate(
            self.arguments(family="relational_selectivity", rows=20, filter_selectivity=0.25)
        )
        observed = {}
        for form, expected_calls in (("udf_before_filter", 20), ("filter_before_udf", 5)):
            adapter = SQLiteAdapter()
            adapter.setup()
            adapter.load_data(rows)
            adapter.register_udf((2, 3))
            result = adapter.run_query(form)
            metrics = adapter.collect_metrics()
            adapter.cleanup()
            self.assertEqual(metrics["udf_invocations"], expected_calls)
            self.assertEqual(metrics["observed_loop_iterations"], expected_calls * 6)
            observed[form] = result
        self.assertEqual(observed["udf_before_filter"], observed["filter_before_udf"])


if __name__ == "__main__":
    unittest.main()
