#!/usr/bin/env bash
set -euo pipefail

image="${1:?the hive worker image repository is required}"
release="${2:?the release tag is required}"
latest="${3:?whether the release is the newest is required}"
ref="${4:?the agentihooks revision is required}"
version="${release#v}"

published() {
  local status
  if status="$(docker buildx imagetools inspect "$1" 2>&1 >/dev/null)"; then
    return 0
  elif [[ "$status" != *"not found"* ]]; then
    echo "$status" >&2
    exit 1
  fi
  return 1
}

if published "$image:$version"; then
  echo "$image:$version is already published; pushing nothing"
  exit 0
fi

if ! published "$image:$ref"; then
  docker build -f deploy/worker/Dockerfile \
    --build-arg WORKER_IMAGE="ghcr.io/the-cloud-clockwork/agentihooks-worker:$version" \
    --build-arg AGENTIHOOKS_REF="$ref" \
    --build-arg AGENTIHOOKS_VERSION="$version" \
    --tag "$image:$ref" .
  docker run --rm --tmpfs /home/worker:uid=10001,gid=10001 --entrypoint bash "$image:$ref" \
    -c 'agentihooks init --profile engineer && grep -qx "<!-- profile: engineer -->" "$HOME/.claude/CLAUDE.md"'
  docker push "$image:$ref"
fi
if [[ "$latest" == true ]]; then
  newest="$(git ls-remote --tags --refs origin | sed -n 's#^[0-9a-f]*\trefs/tags/\(v[0-9]*\.[0-9]*\.[0-9]*\)$#\1#p' | sort -V | tail -n 1)"
  if [[ "$newest" == "$release" ]]; then
    docker buildx imagetools create --tag "$image:release" "$image:$ref"
  else
    echo "the newest release is $newest, not $release; leaving the release tag where it is"
  fi
fi
docker buildx imagetools create --tag "$image:$version" "$image:$ref"
