#!/usr/bin/env bash
set -euo pipefail

CODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTIFACT_ROOT="$(cd "${CODE_DIR}/../../.." && pwd)"
OUTPUT_DIR="${STUDY_OUTPUT_DIR:-/tmp/e3-mapping-output}"
IMAGE_TAG="representation-e3-pg14:ubuntu22.04"

docker build --build-arg "HOST_UID=$(id -u)" --build-arg "HOST_GID=$(id -g)" \
  --tag "${IMAGE_TAG}" --file "${CODE_DIR}/Dockerfile" "${CODE_DIR}"
mkdir -p "${OUTPUT_DIR}"
chmod 0777 "${OUTPUT_DIR}"
docker run --rm --init \
  --user experiment \
  --volume "${ARTIFACT_ROOT}:${ARTIFACT_ROOT}:ro" \
  --volume "${OUTPUT_DIR}:${OUTPUT_DIR}" \
  --workdir "${ARTIFACT_ROOT}" \
  --env "STUDY_OUTPUT_DIR=${OUTPUT_DIR}" \
  --env "HOME=/home/experiment" \
  --env "USER=experiment" \
  "${IMAGE_TAG}" \
  bash -lc "mkdir -p '${OUTPUT_DIR}/raw/extension' && \
    gcc -shared -fPIC -O2 -I /usr/include/postgresql/14/server \
      -o '${OUTPUT_DIR}/raw/extension/task36_mapping_support.so' \
      '${ARTIFACT_ROOT}/studies/e3_mapping_sensitivity/code/task36_mapping_support.c' && \
    python3 '${ARTIFACT_ROOT}/studies/e3_mapping_sensitivity/code/run_experiment.py'"
