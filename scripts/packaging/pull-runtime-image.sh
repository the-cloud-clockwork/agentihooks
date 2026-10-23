#!/usr/bin/env bash
set -euo pipefail

image="$1"
shift
docker_host="${DOCKER_HOST:-unix:///var/run/docker.sock}"
attempts=3
limit="${PULL_ATTEMPT_SECONDS:-120}"
if [[ ${SWARM_SMOKE_RUNTIME_IMAGES_READY:-} == true &&
      ( "$image" == mirror.gcr.io/library/redis:7 || "$image" == mirror.gcr.io/library/redis:7-alpine ||
        "$image" == mirror.gcr.io/library/nats:2.15.1-alpine ) ]] &&
   [[ "$(timeout "$limit" docker --host "$docker_host" image inspect --format '{{.Architecture}}' "$image" 2>/dev/null)" == amd64 ]]; then
  exit 0
fi
acquire() {
  manifest="$(timeout "$limit" docker manifest inspect "$@" "$image")" && digest="$(python3 -c '
import json
import sys

index = json.load(sys.stdin)
print(next(manifest["digest"] for manifest in index["manifests"]
           if manifest.get("platform", {}).get("os") == "linux"
           and manifest["platform"].get("architecture") == "amd64"))
' <<<"$manifest")" && timeout "$limit" docker --host "$docker_host" pull "$image@$digest"
}
for attempt in $(seq "$attempts"); do
  if acquire "$@"; then
    break
  else
    status=$?
  fi
  if (( attempt == attempts )); then
    exit "$status"
  fi
  printf 'Pull of %s failed on attempt %s of %s; retrying in 10 seconds\n' "$image" "$attempt" "$attempts" >&2
  sleep 10
done
docker --host "$docker_host" tag "$image@$digest" "$image"
