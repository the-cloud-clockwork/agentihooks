#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
image=$1
expected="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["tools"]["herdr"]["version_output"])' \
  docker/swarm-node/versions.lock)"
observed="$(docker run --rm --entrypoint herdr "$image" --version 2>&1)" || true
if [[ $observed != "$expected" ]]; then
  printf 'the swarm image herdr client answered %s; the worker lock pins %s\n' "$observed" "$expected" >&2
  exit 1
fi
printf 'swarm image herdr client: %s, the version the worker lock pins\n' "$observed"
machines="$(docker run --rm --entrypoint herdr "$image" machine list --json 2>&1)" || true
if [[ $machines != "[]" ]]; then
  printf 'the swarm image herdr client could not list saved machines as the controller user: %s\n' "$machines" >&2
  exit 1
fi
printf 'swarm image herdr client lists saved machines as the controller user: %s\n' "$machines"
