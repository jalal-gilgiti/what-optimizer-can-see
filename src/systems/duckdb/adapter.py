"""DuckDB capability probe for the installed legacy Python module.

DuckDB 0.6.1 in this environment lacks DuckDBPyConnection.create_function,
so the controlled scalar-UDF runner deliberately does not emulate that feature.
"""

from __future__ import annotations


def capability() -> dict[str, object]:
    try:
        import duckdb
    except Exception as exc:
        return {"available": False, "reason": repr(exc)}
    return {
        "available": True,
        "version": duckdb.__version__,
        "python_scalar_udf_registration": hasattr(duckdb.DuckDBPyConnection, "create_function"),
    }
