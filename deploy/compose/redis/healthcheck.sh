#!/usr/bin/env bash
set -euo pipefail

args=(--user swarm --pass "$SWARM_REDIS_PASSWORD" --no-auth-warning)
if [[ "$SWARM_REDIS_SCHEME" == rediss ]]; then
  args+=(--tls --cacert /run/swarm-tls/ca.pem)
fi
[[ "$(redis-cli "${args[@]}" ping)" == PONG ]]
