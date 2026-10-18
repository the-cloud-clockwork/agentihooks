#!/usr/bin/env bash
set -euo pipefail

base="${1:?the published worker base image is required}"
revision="$(docker pull -q --platform linux/amd64 "$base" > /dev/null && docker run --rm --entrypoint python "$base" -c 'import json; print(json.load(open("/opt/swarm-node/manifest.json"))["source_revision"])')" \
  || { echo "the published worker base revision cannot be read" >&2; exit 2; }
git fetch -q --depth=1 origin "$revision" \
  || { echo "the published worker base revision $revision cannot be fetched" >&2; exit 2; }
git diff --quiet "$revision" HEAD -- docker/swarm-node scripts/swarm_v2/worker_image.py profiles docs/swarm-v2/schemas \
  || { echo "the worker base inputs changed since $revision" >&2; exit 1; }
