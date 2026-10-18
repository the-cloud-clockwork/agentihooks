#!/usr/bin/env bash
set -euo pipefail

args=(--user swarm --no-auth-warning)
if [[ "$SWARM_REDIS_SCHEME" == rediss ]]; then
  args+=(--tls --cacert /run/swarm-tls/ca.pem)
fi
[[ "$(REDISCLI_AUTH="$SWARM_REDIS_PASSWORD" redis-cli "${args[@]}" ping)" == PONG ]]
