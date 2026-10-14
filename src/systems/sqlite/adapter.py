"""In-memory SQLite adapter for the controlled scalar-UDF smoke path."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any

from systems.base import SystemAdapter
from workloads.synthetic.model import fixed_loop_checksum_observed


class SQLiteAdapter(SystemAdapter):
    QUERY = "SELECT COUNT(*), SUM(fixed_loop(value)) FROM input_rows"
    E2_QUERIES = {
        "udf_before_filter": (
            "SELECT COUNT(*), SUM(udf_value) FROM ("
            "SELECT retained, fixed_loop(value) AS udf_value "
            "FROM input_rows LIMIT -1 OFFSET 0"
            ") WHERE retained = 1"
        ),
        "filter_before_udf": (
            "SELECT COUNT(*), SUM(fixed_loop(value)) "
            "FROM input_rows WHERE retained = 1"
        ),
    }

    def __init__(self) -> None:
        self.connection: sqlite3.Connection | None = None
        self.udf_invocations = 0
        self.observed_loop_iterations = 0

    @property
    def system_version(self) -> str:
        return sqlite3.sqlite_version

    def setup(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute(
            "CREATE TABLE input_rows (id INTEGER, value INTEGER, flag INTEGER, "
            "loop_count INTEGER, retained INTEGER, lookup_key INTEGER)"
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
        self.connection.commit()

    def register_udf(self, bounds: tuple[int, ...]) -> None:
        assert self.connection is not None

        def fixed_loop(value: int) -> int:
            self.udf_invocations += 1
            checksum, observed = fixed_loop_checksum_observed(value, bounds)
            self.observed_loop_iterations += observed
            return checksum

        try:
            self.connection.create_function("fixed_loop", 1, fixed_loop, deterministic=True)
        except TypeError:
            self.connection.create_function("fixed_loop", 1, fixed_loop)

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
        rows = self.connection.execute("EXPLAIN QUERY PLAN " + query).fetchall()
        return "\n".join(" | ".join(str(value) for value in row) for row in rows)

    def collect_metrics(self) -> dict[str, Any]:
        return {
            "udf_invocations": self.udf_invocations,
            "observed_loop_iterations": self.observed_loop_iterations,
        }

    def cleanup(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
