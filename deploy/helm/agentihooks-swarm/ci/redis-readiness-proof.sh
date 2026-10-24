#!/usr/bin/env bash
set -euo pipefail

release=$1
ledger_pod="$release-ledger-0"
redis_pod="$release-redis-0"
original="$(kubectl get pod "$redis_pod" -o jsonpath='{.metadata.uid}')"
image="$(kubectl get pod "$redis_pod" -o jsonpath='{.spec.containers[0].image}')"
patch="$(python3 -c 'import json, sys; print(json.dumps({"spec": {"template": {"spec": {"initContainers": [{"name": "startup-delay", "image": sys.argv[1], "command": ["sh", "-c", "sleep 15"]}]}}}}))' "$image")"
kubectl patch statefulset "$release-redis" --type=strategic -p "$patch"
replaced=""
for _ in $(seq 60); do
  current="$(kubectl get pod "$redis_pod" --ignore-not-found -o jsonpath='{.metadata.uid}')"
  if [[ -n $current && $current != "$original" ]]; then
    replaced=yes
    break
  fi
  sleep 1
done
if [[ -z $replaced ]]; then
  printf 'the Redis delay plant never replaced its pod\n' >&2
  exit 1
fi
kubectl exec -i "$ledger_pod" -- python - <<'READY'
import os
import time

from redis import Redis, RedisError

client = Redis.from_url(os.environ["AGENTIHOOKS_SWARM_REDIS_URL"], socket_connect_timeout=1, socket_timeout=1)
started = time.monotonic()
deadline = started + 60
failures = 0
while True:
    try:
        client.ping()
        break
    except RedisError:
        failures += 1
        if time.monotonic() >= deadline:
            raise SystemExit("Redis did not become reachable from the ledger within sixty seconds") from None
        time.sleep(1)
assert failures, "the Redis delay plant never made Redis unavailable to the ledger"
print(f"Redis readiness waited through the planted delay: {failures} failed pings, {time.monotonic() - started:.1f}s")
READY
kubectl rollout status statefulset "$release-redis" --timeout 1m
