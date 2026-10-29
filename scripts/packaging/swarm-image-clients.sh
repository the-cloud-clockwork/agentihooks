#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
image=$1
locked() { python3 -c 'import functools, json, sys; print(functools.reduce(lambda found, key: found[key], sys.argv[2:], json.load(open(sys.argv[1]))))' docker/swarm-node/versions.lock "$@"; }
expected="$(locked tools herdr version_output)"
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
expected="$(locked shell_packages openssh-client)"
observed="$(docker run --rm --entrypoint dpkg-query "$image" -W -f '${Version}' openssh-client 2>&1)" || true
if [[ $observed != "$expected" ]]; then
  printf 'the swarm image ssh client package is %s; the worker lock pins %s\n' "$observed" "$expected" >&2
  exit 1
fi
version="$(docker run --rm --entrypoint /usr/bin/ssh "$image" -V 2>&1)" || {
  printf 'the swarm image ssh client did not run: %s\n' "$version" >&2
  exit 1
}
printf 'swarm image ssh client: openssh-client %s, the version the worker lock pins (%s)\n' "$observed" "$version"
