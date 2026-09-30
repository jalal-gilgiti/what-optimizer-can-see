#!/usr/bin/env bash
set -euo pipefail

: "${Q10_SOURCE_DB:?set Q10_SOURCE_DB to the TPC-H SF1 DuckDB database}"
: "${DUCKDB_CLI:?set DUCKDB_CLI to the DuckDB executable}"

CODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${STUDY_OUTPUT_DIR:-/tmp/natural-q10-output}"
PG_CONFIG_BIN="${PG_CONFIG:-/usr/lib/postgresql/14/bin/pg_config}"
INCLUDE_DIR="$(${PG_CONFIG_BIN} --includedir-server)"

mkdir -p "${OUTPUT_DIR}/raw/extension"
gcc -shared -fPIC -O2 -I "${INCLUDE_DIR}" \
  -o "${OUTPUT_DIR}/raw/extension/q10_support.so" "${CODE_DIR}/q10_support.c"

STUDY_OUTPUT_DIR="${OUTPUT_DIR}" \
Q10_SOURCE_DB="${Q10_SOURCE_DB}" \
DUCKDB_CLI="${DUCKDB_CLI}" \
python3 "${CODE_DIR}/run_experiment_portable.py"
