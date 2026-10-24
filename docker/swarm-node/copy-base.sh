#!/usr/bin/env bash
set -euo pipefail

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=docker/swarm-node/base-copy.sh
source "$source_dir/base-copy.sh"
if docker buildx imagetools inspect "$copy" > /dev/null 2>&1; then
    printf 'The registry copy already holds %s\n' "$digest"
else
    trap 'docker logout ghcr.io' EXIT
    printf '%s' "${GITHUB_TOKEN:?the job token writes the registry copy}" \
        | docker login ghcr.io -u "${GITHUB_ACTOR:?}" --password-stdin
    docker buildx imagetools create --tag "$copy_repository:${digest/:/-}" "$base"
    docker logout ghcr.io
    trap - EXIT
fi
if ! docker buildx imagetools inspect "$copy"; then
    printf 'anonymous pulls cannot read %s; make the package public\n' "$copy" >&2
    exit 1
fi
