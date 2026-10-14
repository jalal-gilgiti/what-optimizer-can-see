#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SURVEY_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
IMAGE_TAG="e3-mapping-sensitivity-pg14:ubuntu22.04"

docker run --rm --init \
  --volume "${SURVEY_ROOT}:${SURVEY_ROOT}" \
  --workdir "${SURVEY_ROOT}" \
  --env "SURVEY_ROOT=${SURVEY_ROOT}" \
  "${IMAGE_TAG}" bash -lc '
    set -euo pipefail
    dpkg -i \
      "$SURVEY_ROOT/experiments/runtime/postgresql/postgresql-20260829T084826Z/packages/libpq5_14.24-0ubuntu0.22.04.1_amd64.deb" \
      "$SURVEY_ROOT/experiments/runtime/postgresql/postgresql-20260829T084826Z/packages/postgresql-client-14_14.24-0ubuntu0.22.04.1_amd64.deb" \
      "$SURVEY_ROOT/experiments/runtime/postgresql/postgresql-20260829T084826Z/packages/postgresql-14_14.24-0ubuntu0.22.04.1_amd64.deb" \
      "$SURVEY_ROOT/experiments/runtime/task15b_live_e3/packages/postgresql-server-dev-14_14.24-0ubuntu0.22.04.1_amd64.deb" >/tmp/q10-dpkg.log
    chmod +x "$SURVEY_ROOT/experiments/runtime/postgresql/postgresql-20260829T084826Z/root/usr/lib/postgresql/14/bin/"*
    mkdir -p /tmp/q10-home "$SURVEY_ROOT/reviewer_strengthening/11_natural_benchmark_q10/raw/extension"
    chown experiment:experiment /tmp/q10-home
    chmod -R 0777 "$SURVEY_ROOT/reviewer_strengthening/11_natural_benchmark_q10"
    gcc -shared -fPIC -O2 -Wall -Wextra -Wno-unused-parameter \
      -I /usr/include/postgresql/14/server \
      -o "$SURVEY_ROOT/reviewer_strengthening/11_natural_benchmark_q10/raw/extension/q10_support.so" \
      "$SURVEY_ROOT/reviewer_strengthening/11_natural_benchmark_q10/q10_support.c"
    runuser -u experiment -- env HOME=/tmp/q10-home USER=experiment LOGNAME=experiment \
      LC_ALL=C.UTF-8 LANG=C.UTF-8 SURVEY_ROOT="$SURVEY_ROOT" \
      python3 "$SURVEY_ROOT/reviewer_strengthening/11_natural_benchmark_q10/run_experiment.py"
  '

