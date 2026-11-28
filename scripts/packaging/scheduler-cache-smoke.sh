#!/usr/bin/env bash
set -euo pipefail

image="${SWARM_SMOKE_IMAGE:-ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev}"
name="scheduler-cache-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
cache_volume="${name}-cache"
proof_volume="${name}-proof"
cleanup() {
  docker rm -fv "$name" >/dev/null 2>&1 || true
  docker volume rm "$cache_volume" "$proof_volume" >/dev/null || echo "Cleanup retained $cache_volume or $proof_volume" >&2
}
docker volume create "$cache_volume" >/dev/null
docker volume create "$proof_volume" >/dev/null
trap cleanup EXIT
docker run --rm -i --network none --user 0:0 --entrypoint python \
  -v "$cache_volume:/cache" -v "$proof_volume:/proof" "$image" \
  -c 'import os,sys; from pathlib import Path; Path("/proof/scheduler_cache_probe.py").write_text(sys.stdin.read()); os.chown("/cache",10001,10001); os.chmod("/cache",0o750); os.chown("/proof",10001,10001)' \
  < tests/ci_lab/scheduler_cache_probe.py
run=(--network none --read-only --cap-drop ALL --security-opt no-new-privileges --user 10001:10001
  --entrypoint python -v "$cache_volume:/cache" -v "$proof_volume:/proof"
  -e CI_LAB_CACHE_ROOT=/cache -e CI_LAB_CACHE_LIMIT_BYTES=1048576
  -e CI_LAB_CACHE_POD_UID=11111111-1111-4111-8111-111111111111)
docker run -d --name "$name" "${run[@]}" "$image" /proof/scheduler_cache_probe.py breach >/dev/null
status="$(timeout 45 docker wait "$name")"
docker logs "$name"
[[ "$status" != 0 ]]
[[ "$(docker inspect --format '{{.State.Running}}' "$name")" == false ]]
docker run --rm "${run[@]}" "$image" /proof/scheduler_cache_probe.py verify
docker run --rm "${run[@]}" "$image" /proof/scheduler_cache_probe.py restart
docker run --rm "${run[@]}" "$image" /proof/scheduler_cache_probe.py reused
printf 'Scheduler cache PID-one, detached-child cleanup and restart proof passed\n'
