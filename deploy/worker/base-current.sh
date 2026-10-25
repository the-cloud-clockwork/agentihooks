#!/usr/bin/env bash
set -euo pipefail

base="${1:?the published worker base image is required}"
pinned="${2:?the file that receives the pinned worker base is required}"
undetermined() {
  echo "::warning title=Worker hive base::$1; rebuilding the worker base"
  exit 2
}
found="$(python3 "$(dirname "$0")/base-revision.py" "$base")" \
  || undetermined "the published worker base revision cannot be read"
read -r image revision <<< "$found"
git fetch -q --depth=1 origin "$revision" \
  || undetermined "the published worker base revision $revision cannot be fetched"
status=0
git diff --quiet "$revision" HEAD -- docker/swarm-node scripts/swarm_v2/worker_image.py profiles docs/swarm-v2/schemas || status=$?
if [[ "$status" == 0 ]]; then
  printf '%s\n' "$image" > "$pinned"
  exit 0
fi
[[ "$status" != 1 ]] || { echo "the worker base inputs changed since $revision"; exit 1; }
undetermined "the worker base inputs cannot be compared with revision $revision"
