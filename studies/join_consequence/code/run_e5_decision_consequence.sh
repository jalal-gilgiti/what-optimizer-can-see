#!/usr/bin/env bash
set -euo pipefail

CODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTIFACT_ROOT="$(cd "${CODE_DIR}/../../.." && pwd)"
OUTPUT_DIR="${STUDY_OUTPUT_DIR:-/tmp/join-consequence-output}"
IMAGE_TAG="representation-join-pg14:ubuntu22.04"

docker build --build-arg "HOST_UID=$(id -u)" --build-arg "HOST_GID=$(id -g)" \
  --tag "${IMAGE_TAG}" --file "${CODE_DIR}/Dockerfile" "${CODE_DIR}"
mkdir -p "${OUTPUT_DIR}"
chmod 0777 "${OUTPUT_DIR}"
docker run --rm --init \
  --user experiment \
  --volume "${ARTIFACT_ROOT}:${ARTIFACT_ROOT}:ro" \
  --volume "${OUTPUT_DIR}:${OUTPUT_DIR}" \
  --workdir "${ARTIFACT_ROOT}" \
  --env "STUDY_SOURCE_DIR=${ARTIFACT_ROOT}/studies/join_consequence" \
  --env "STUDY_OUTPUT_DIR=${OUTPUT_DIR}" \
  --env "E5DC_STAGE=${E5DC_STAGE:-full}" \
  --env "HOME=/home/experiment" \
  --env "USER=experiment" \
  "${IMAGE_TAG}" \
  bash -lc "mkdir -p '${OUTPUT_DIR}/raw/extension' && \
    gcc -shared -fPIC -O2 -I /usr/include/postgresql/14/server \
      -o '${OUTPUT_DIR}/raw/extension/e5dc_support.so' \
      '${ARTIFACT_ROOT}/studies/join_consequence/code/e5dc_support.c' && \
    cd '${ARTIFACT_ROOT}/studies/join_consequence/code' && \
    python3 run_experiment.py"
