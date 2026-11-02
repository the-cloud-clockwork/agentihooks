#!/usr/bin/env bash
set -euo pipefail

for attempt in 1 2 3; do
  node_source="$KIND_NODE_IMAGE"
  if (( attempt == 2 )); then node_source="docker.io/${KIND_NODE_IMAGE#mirror.gcr.io/}"; fi
  if timeout 300 docker pull -q "$node_source" >/dev/null; then
    printf '%s\n' "$node_source"
    exit 0
  fi
  printf 'Pull of %s failed on attempt %s of 3\n' "$node_source" "$attempt" >&2
  if (( attempt < 3 )); then sleep 10; fi
done
exit 1
