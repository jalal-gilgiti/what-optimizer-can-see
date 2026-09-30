# Pre-execution implementation amendment 01

The first invocation stopped while compiling `q10_support.c`; no database was
created and no plan, estimate, semantic result, or timing was observed. The
compiler reported that `date2j` and `cpu_operator_cost` were undeclared.

Only the PostgreSQL declaration headers were added:

- `utils/datetime.h` for `date2j`;
- `optimizer/optimizer.h` for `cpu_operator_cost`.

The preregistered workload, data, representations, grid, endpoints, statistics,
success criteria, and stopping rule are unchanged.
