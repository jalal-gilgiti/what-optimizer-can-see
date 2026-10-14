#!/usr/bin/env bash
set -euo pipefail

PRISM_REPOSITORY="https://github.com/SamArch27/PRISM.git"
PRISM_COMMIT="689902595ba2ec8f86cbd5eb952b5fde09906e50"
DUCKDB_REPOSITORY="https://github.com/hkulyc/duckdb.git"
DUCKDB_COMMIT="dfb68aa3e8b504c1d19555f97035a5a00a1e2df4"

if [[ $# -ne 1 ]]; then
  echo "usage: $0 OUTPUT_DIRECTORY" >&2
  echo "Set DUCKDB_CLI to a previously built pinned CLI to skip the source build." >&2
  exit 2
fi

OUTPUT_DIR="$(realpath -m "$1")"
case "${OUTPUT_DIR}" in
  *"'"*) echo "output path must not contain a single quote" >&2; exit 2 ;;
esac
mkdir -p "${OUTPUT_DIR}"

if [[ -n "${DUCKDB_CLI:-}" ]]; then
  DUCKDB_BIN="$(realpath "${DUCKDB_CLI}")"
  [[ -x "${DUCKDB_BIN}" ]] || { echo "DUCKDB_CLI is not executable: ${DUCKDB_BIN}" >&2; exit 2; }
else
  for command in git make python3 g++ sha256sum; do
    command -v "${command}" >/dev/null || { echo "missing required command: ${command}" >&2; exit 2; }
  done
  SOURCE_DIR="${OUTPUT_DIR}/source"
  TOOLS_DIR="${OUTPUT_DIR}/tools_env"
  [[ ! -e "${SOURCE_DIR}" && ! -e "${TOOLS_DIR}" ]] || {
    echo "refusing to overwrite existing source or tools directory under ${OUTPUT_DIR}" >&2
    exit 2
  }
  git clone --filter=blob:none "${PRISM_REPOSITORY}" "${SOURCE_DIR}/prism"
  git -C "${SOURCE_DIR}/prism" checkout --detach "${PRISM_COMMIT}"
  git clone --branch cherry_pick --single-branch "${DUCKDB_REPOSITORY}" \
    "${SOURCE_DIR}/prism/duckdb"
  git -C "${SOURCE_DIR}/prism/duckdb" checkout --detach "${DUCKDB_COMMIT}"
  python3 -m venv "${TOOLS_DIR}"
  "${TOOLS_DIR}/bin/python" -m pip install --disable-pip-version-check cmake==3.31.6
  env PATH="${TOOLS_DIR}/bin:${PATH}" make -C "${SOURCE_DIR}/prism" release
  DUCKDB_BIN="${SOURCE_DIR}/prism/build/release/duckdb"
fi

DATABASE="${OUTPUT_DIR}/tpch_sf1.duckdb"
PROJECTIONS="${OUTPUT_DIR}/verified_projections"
[[ ! -e "${DATABASE}" && ! -e "${PROJECTIONS}" ]] || {
  echo "refusing to overwrite an existing database or projection directory" >&2
  exit 2
}
mkdir -p "${PROJECTIONS}"

"${DUCKDB_BIN}" "${DATABASE}" \
  -c "SET threads=1; CALL dbgen(sf=1); CHECKPOINT;"

"${DUCKDB_BIN}" -readonly "${DATABASE}" -c "
COPY (SELECT c_custkey,c_name,c_acctbal,c_nationkey,c_address,c_phone,c_comment FROM customer)
  TO '${PROJECTIONS}/customer.csv' (FORMAT CSV, HEADER FALSE);
COPY (SELECT o_orderkey,o_custkey,o_orderdate FROM orders)
  TO '${PROJECTIONS}/orders.csv' (FORMAT CSV, HEADER FALSE);
COPY (SELECT l_orderkey,l_extendedprice,l_discount,l_returnflag FROM lineitem)
  TO '${PROJECTIONS}/lineitem.csv' (FORMAT CSV, HEADER FALSE);
COPY (SELECT n_nationkey,n_name FROM nation)
  TO '${PROJECTIONS}/nation.csv' (FORMAT CSV, HEADER FALSE);"

check_hash() {
  local expected="$1"
  local path="$2"
  local actual
  actual="$(sha256sum "${path}" | cut -d' ' -f1)"
  if [[ "${actual}" != "${expected}" ]]; then
    echo "projection hash mismatch: ${path}" >&2
    echo "expected ${expected}" >&2
    echo "actual   ${actual}" >&2
    exit 1
  fi
}

check_hash "5ac79d370191280102739d377bd9c4be532390e13b791d1793e7d42b322e0a00" \
  "${PROJECTIONS}/customer.csv"
check_hash "95d7625e8decdfa804f354178c8718b2acfa842464c3f0d28fc7b4e6394b31fe" \
  "${PROJECTIONS}/lineitem.csv"
check_hash "1d989fb40027f21f04c0890e11b5a0724e61dc1b0f2243645f8befc74571552a" \
  "${PROJECTIONS}/nation.csv"
check_hash "9d539e6e7c67565181cc5c738763476f234bffba5e11af3bc052e2770348582c" \
  "${PROJECTIONS}/orders.csv"

echo "Q10 input preparation: PASS"
echo "Q10_SOURCE_DB=${DATABASE}"
echo "DUCKDB_CLI=${DUCKDB_BIN}"
echo "database_sha256=$(sha256sum "${DATABASE}" | cut -d' ' -f1)"
echo "The physical database hash may differ from the historical file; all four"
echo "experiment-consumed projections match the frozen byte-level hashes."
