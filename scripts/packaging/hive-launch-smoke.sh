#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
export COMPOSE_PROJECT_NAME="$(python3 -c 'import uuid; print("hive-launch-" + uuid.uuid4().hex[:12])')"
fixture=tests/integration/hive_launch/compose.yaml
cleanup() {
  docker compose -f "$fixture" down --volumes --remove-orphans
}
trap cleanup EXIT
docker compose -f "$fixture" up --build --wait --wait-timeout 180 redis worker
docker compose -f "$fixture" run --rm controller
printf 'Remote hive scratch agent launch passed\n'
