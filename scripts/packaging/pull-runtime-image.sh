#!/usr/bin/env bash
set -euo pipefail

image="$1"
shift
docker_host="${DOCKER_HOST:-unix:///var/run/docker.sock}"
attempts=3
limit="${PULL_ATTEMPT_SECONDS:-120}"
delay="${PULL_RETRY_SECONDS:-10}"
if [[ ${SWARM_SMOKE_RUNTIME_IMAGES_READY:-} == true &&
      ( "$image" == mirror.gcr.io/library/redis:7 || "$image" == mirror.gcr.io/library/redis:7-alpine ||
        "$image" == mirror.gcr.io/library/nats:2.15.1-alpine ) ]] &&
   [[ "$(timeout "$limit" docker --host "$docker_host" image inspect --format '{{.Architecture}}' "$image" 2>/dev/null)" == amd64 ]]; then
  exit 0
fi
fallback="$image"
if [[ "$image" == public.ecr.aws/docker/library/* ]]; then
  fallback="mirror.gcr.io/library/${image#public.ecr.aws/docker/library/}"
elif [[ "$image" == mirror.gcr.io/library/* ]]; then
  fallback="public.ecr.aws/docker/library/${image#mirror.gcr.io/library/}"
fi
acquire() {
  manifest="$(timeout "$limit" docker manifest inspect "$@" "$source")" && digest="$(python3 -c '
import json
import sys

index = json.load(sys.stdin)
print(next(manifest["digest"] for manifest in index["manifests"]
           if manifest.get("platform", {}).get("os") == "linux"
           and manifest["platform"].get("architecture") == "amd64"))
' <<<"$manifest")" && timeout "$limit" docker --host "$docker_host" pull "$source@$digest"
}
for attempt in $(seq "$attempts"); do
  source="$image"
  if (( attempt % 2 == 0 )); then source="$fallback"; fi
  if acquire "$@"; then
    break
  else
    status=$?
  fi
  if (( attempt == attempts )); then
    exit "$status"
  fi
  printf 'Pull of %s failed on attempt %s of %s; retrying in %s seconds\n' "$image" "$attempt" "$attempts" "$delay" >&2
  sleep "$delay"
done
docker --host "$docker_host" tag "$source@$digest" "$image"
