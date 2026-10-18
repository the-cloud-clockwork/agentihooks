#!/usr/bin/env bash
set -euo pipefail

base="${1:?the published worker base image is required}"
docker pull -q --platform linux/amd64 "$base" > /dev/null
revision="$(docker run --rm --entrypoint python "$base" -c 'import json; print(json.load(open("/opt/swarm-node/manifest.json"))["source_revision"])')"
git fetch -q --depth=1 origin "$revision"
git diff --quiet "$revision" HEAD -- docker/swarm-node scripts/swarm_v2/worker_image.py
