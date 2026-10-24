#!/usr/bin/env bash
set -euo pipefail

assets="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
stack="${SWARM_STACK_HOME:-$(cd "$assets/../.." && pwd)}"
address="$(tailscale ip -4 2>/dev/null || true)"
if [[ ! "$address" =~ ^100\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] \
  || (( 10#${BASH_REMATCH[1]:-0} < 64 || 10#${BASH_REMATCH[1]:-0} > 127 \
     || 10#${BASH_REMATCH[2]:-0} > 255 || 10#${BASH_REMATCH[3]:-0} > 255 )); then
  echo 'No current tailnet IPv4 address; connect Tailscale before starting the fronts.' >&2
  exit 1
fi
export SWARM_TAILNET_IP="$address"
export SWARM_TAILNET_ASSETS="$assets"
if [[ "$#" == 0 ]]; then
  set -- up --detach --no-deps --force-recreate swarm-tailnet hive-tailnet redis-tailnet
fi
exec docker compose --project-name "${SWARM_COMPOSE_PROJECT:-agentihooks-swarm}" \
  --project-directory "$stack" --env-file "$stack/.env" \
  --file "$stack/compose.yaml" --file "$stack/deploy/compose/adopt.yaml" \
  --file "$assets/tailnet.yaml" "$@"
