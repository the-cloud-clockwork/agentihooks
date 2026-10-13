#!/usr/bin/env bash
set -euo pipefail

image="$1"
shift
docker_host="${DOCKER_HOST:-unix:///var/run/docker.sock}"
digest="$(docker manifest inspect "$@" "$image" | python3 -c '
import json
import sys

index = json.load(sys.stdin)
print(next(manifest["digest"] for manifest in index["manifests"]
           if manifest.get("platform", {}).get("os") == "linux"
           and manifest["platform"].get("architecture") == "amd64"))
')"
docker --host "$docker_host" pull "$image@$digest"
docker --host "$docker_host" tag "$image@$digest" "$image"
