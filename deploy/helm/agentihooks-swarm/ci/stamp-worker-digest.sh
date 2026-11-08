#!/usr/bin/env bash
set -euo pipefail

values="${1:?the chart values file is required}"
image="${2:?the hive worker image repository is required}"
tag="${3:?the image tag is required}"

repository="${image#ghcr.io/}"
token="$(curl -fsS --retry 5 --retry-connrefused \
  "https://ghcr.io/token?service=ghcr.io&scope=repository:$repository:pull" | jq -r .token)"
digest="$(curl -fsSI --retry 5 --retry-connrefused -H "Authorization: Bearer $token" \
  -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json" \
  "https://ghcr.io/v2/$repository/manifests/$tag" | tr -d '\r' | sed -n 's/^docker-content-digest: *//Ip')"
if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  printf 'no digest for %s:%s\n' "$image" "$tag" >&2
  exit 1
fi
if [[ "$(grep -c '^    imageDigest: ""$' "$values")" != 1 ]]; then
  printf '%s needs exactly one empty workers.rollout.imageDigest line\n' "$values" >&2
  exit 1
fi
sed -i "s/^    imageDigest: \"\"$/    imageDigest: \"$digest\"/" "$values"
printf 'stamped %s:%s as %s\n' "$image" "$tag" "$digest"
