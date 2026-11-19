#!/usr/bin/env bash
set -euo pipefail

image="${1:?the hive worker image repository is required}"
release="${2:?the release tag is required}"
ref="${3:?the agentihooks revision is required}"
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
if [[ "$release" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  tags="$(git ls-remote --tags --refs origin)"
  newest_first="$(sed -n 's#^[0-9a-f]*\trefs/tags/\(v[0-9]*\.[0-9]*\.[0-9]*\)$#\1#p' <<<"$tags" | sort -rV)"
  while read -r -u 3 tag; do
    if [[ "$tag" == "$release" ]]; then
      docker buildx imagetools create --tag "$image:release" "$image:$ref"
      break
    elif published "$image:${tag#v}"; then
      echo "the newest published release is $tag, not $release; leaving the release tag where it is"
      break
    fi
  done 3<<<"$newest_first"
else
  echo "$release is not a stable release; leaving the release tag where it is"
fi
docker buildx imagetools create --tag "$image:$version" "$image:$ref"
