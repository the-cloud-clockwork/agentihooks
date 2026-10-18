#!/usr/bin/env bash
set -euo pipefail

base="${1:?the published worker base image is required}"
undetermined() {
  echo "::warning title=Worker hive base::$1; rebuilding the worker base"
  exit 2
}
revision="$(docker pull -q --platform linux/amd64 "$base" > /dev/null && docker run --rm --entrypoint python "$base" -c 'import json; print(json.load(open("/opt/swarm-node/manifest.json"))["source_revision"])')" \
  || undetermined "the published worker base revision cannot be read"
git fetch -q --depth=1 origin "$revision" \
  || undetermined "the published worker base revision $revision cannot be fetched"
status=0
git diff --quiet "$revision" HEAD -- docker/swarm-node scripts/swarm_v2/worker_image.py profiles docs/swarm-v2/schemas || status=$?
[[ "$status" != 0 ]] || exit 0
[[ "$status" != 1 ]] || { echo "the worker base inputs changed since $revision"; exit 1; }
undetermined "the worker base inputs cannot be compared with revision $revision"
