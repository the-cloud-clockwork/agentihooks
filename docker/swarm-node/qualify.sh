#!/usr/bin/env bash
set -euo pipefail

image="${1:?worker image to qualify}"
output="${2:?attestation directory}"
commit="${3:?tested agentihooks commit}"
repo="$(git rev-parse --show-toplevel)"
fixtures="$repo/docker/swarm-node/fixtures/profiles"
mkdir -p "$output"
chmod -R a+rX "$fixtures"
docker run --rm --network none --read-only \
    --tmpfs /home/worker:uid=10001,gid=10001,exec --tmpfs /tmp \
    -v "$fixtures:/opt/probe/profiles:ro" \
    "$image" python -m scripts.swarm_v2.image_probe > "$output/probe.json" 2> "$output/probe.log"
image_id="$(docker image inspect --format '{{.Id}}' "$image")"
cd "$repo"
python3 -m scripts.swarm_v2.image_attestation attest "$output/probe.json" "$image_id" "$commit" \
    "$output/attestation.json"
