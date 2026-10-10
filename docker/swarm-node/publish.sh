#!/usr/bin/env bash
set -euo pipefail

candidate="${1:?qualified local image}"
repository="${2:?registry repository}"
commit="${3:?tested agentihooks commit}"
output="${4:?attestation directory}"
tag="$repository:sha-$commit"
cd "$(git rev-parse --show-toplevel)"

config_of() {
    local raw platform
    raw="$(docker buildx imagetools inspect --raw "$1")"
    if jq -e '.manifests' <<< "$raw" > /dev/null; then
        platform="$(jq -r '[.manifests[] | select(.platform.architecture == "amd64")][0].digest' <<< "$raw")"
        raw="$(docker buildx imagetools inspect --raw "$repository@$platform")"
    fi
    jq -r '.config.digest' <<< "$raw"
}

promote() {
    python3 -m scripts.swarm_v2.image_attestation promote "$output/attestation.json" \
        "$1" "$(config_of "$repository@$1")" "$output/promoted.json" --tag "$tag" "${@:2}"
}

if docker buildx imagetools inspect --format '{{json .Manifest}}' "$tag" > "$output/existing.json" 2> "$output/existing.log"; then
    promote "$(jq -r .digest "$output/existing.json")" --existing
    exit 0
fi
if ! grep -qi "not found" "$output/existing.log"; then
    echo "cannot tell whether $tag exists; nothing pushed" >&2
    exit 1
fi
docker tag "$candidate" "$tag"
docker push "$tag" > "$output/push.log"
reference="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$tag" | grep "^$repository@")"
promote "${reference#*@}"
