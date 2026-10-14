"""DuckDB 1.5.5 adapter for the isolated E1/E2 replication environment."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import duckdb
from duckdb.sqltypes import BIGINT

from systems.base import SystemAdapter
from workloads.synthetic.model import fixed_loop_checksum_observed


class DuckDBExperimentalAdapter(SystemAdapter):
    QUERY = "SELECT COUNT(*), SUM(fixed_loop(value)) FROM input_rows"
    E2_QUERIES = {
        "udf_before_filter": (
            "SELECT COUNT(*), SUM(udf_value) FROM ("
            "SELECT retained, fixed_loop(value) AS udf_value "
            "FROM input_rows OFFSET 0"
            ") WHERE retained = 1"
        ),
        "filter_before_udf": (
            "SELECT COUNT(*), SUM(fixed_loop(value)) "
            "FROM input_rows WHERE retained = 1"
        ),
    }

    def __init__(self) -> None:
        self.connection: duckdb.DuckDBPyConnection | None = None
        self.udf_invocations = 0
        self.observed_loop_iterations = 0

    @property
    def system_version(self) -> str:
        return duckdb.__version__

    def setup(self) -> None:
        self.connection = duckdb.connect(":memory:")
        self.connection.execute(
            "CREATE TABLE input_rows (id BIGINT, value BIGINT, flag BIGINT, "
            "loop_count BIGINT, retained BIGINT, lookup_key BIGINT)"
        )

    def load_data(self, rows: Iterable[dict[str, int]]) -> None:
        assert self.connection is not None
        values = [
            (
                row["id"], row["value"], row["flag"], row["loop_count"],
                row["retained"], row["lookup_key"],
            )
            for row in rows
        ]
        self.connection.executemany("INSERT INTO input_rows VALUES (?, ?, ?, ?, ?, ?)", values)

    def load_data_path(self, path: Path) -> None:
        """Load a frozen CSV using DuckDB's native reader; setup time is not measured."""
        assert self.connection is not None
        self.connection.execute(
            "INSERT INTO input_rows "
            "SELECT id, value, flag, loop_count, retained, lookup_key "
            "FROM read_csv_auto(?, header = true)",
            [str(path)],
        )

    def register_udf(self, bounds: tuple[int, ...]) -> None:
        assert self.connection is not None

        def fixed_loop(value: int) -> int:
            self.udf_invocations += 1
            checksum, observed = fixed_loop_checksum_observed(value, bounds)
            self.observed_loop_iterations += observed
            return checksum

        # The logical function is pure. Internal counters do not affect its result.
        self.connection.create_function(
            "fixed_loop", fixed_loop, [BIGINT], BIGINT, side_effects=False
        )

    def run_query(self, form: str | None = None) -> dict[str, Any]:
        assert self.connection is not None
        self.udf_invocations = 0
        self.observed_loop_iterations = 0
        query = self.E2_QUERIES[form] if form else self.QUERY
        row_count, checksum = self.connection.execute(query).fetchone()
        return {"row_count": row_count, "checksum": checksum}

    def explain_query(self, form: str | None = None) -> str:
        assert self.connection is not None
        query = self.E2_QUERIES[form] if form else self.QUERY
        return self.connection.execute("EXPLAIN " + query).fetchone()[1]

    def collect_metrics(self) -> dict[str, Any]:
        return {
            "udf_invocations": self.udf_invocations,
            "observed_loop_iterations": self.observed_loop_iterations,
        }

    def cleanup(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
