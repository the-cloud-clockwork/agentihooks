#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
export COMPOSE_PROJECT_NAME="$(python3 -c 'import uuid; print("hive-launch-" + uuid.uuid4().hex[:12])')"
fixture=tests/integration/hive_launch/compose.yaml
cleanup() {
  docker compose -f "$fixture" down --volumes --remove-orphans
}
trap cleanup EXIT
bash scripts/packaging/pull-runtime-image.sh mirror.gcr.io/library/redis:7
build=(--build)
if [[ -n "${SWARM_SMOKE_IMAGE:-}" ]]; then
  docker tag "$SWARM_SMOKE_IMAGE" local/agentihooks-hive-proof
  build=(--no-build)
else
  bash scripts/packaging/pull-runtime-image.sh mirror.gcr.io/library/python:3.12-slim-bookworm
fi
docker compose -f "$fixture" up --pull never "${build[@]}" --wait --wait-timeout 180 redis worker
docker compose -f "$fixture" run --rm controller
printf 'Remote hive scratch agent launch passed\n'
