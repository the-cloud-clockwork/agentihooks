#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
run="hive-compose-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
tls="$(mktemp -d)"
export COMPOSE_PROJECT_NAME="$run" COMPOSE_PROFILES=join SWARM_TLS_DIR="$tls" SWARM_REDIS_SCHEME=rediss \
  SWARM_CA_FILE=/tls/ca.pem SWARM_MEMBER_REDIS_URL=rediss://redis:6379/0
SWARM_REDIS_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe())')"
export SWARM_REDIS_PASSWORD
cleanup() {
  docker rm -f "$run-member" >/dev/null 2>&1 || true
  docker compose down --volumes >/dev/null 2>&1 || true
  rm -r -- "$tls"
}
trap cleanup EXIT

openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=$run" -addext "subjectAltName=DNS:redis,DNS:hive" \
  -keyout "$tls/key.pem" -out "$tls/cert.pem" 2>/dev/null
cp "$tls/cert.pem" "$tls/ca.pem"
chmod 0755 "$tls"
chmod 0644 "$tls"/*.pem
if ! docker compose up --build --wait --wait-timeout 180; then
  docker compose logs >&2
  exit 1
fi

slug="compose-proof"
docker compose exec -T -e SLUG="$slug" swarm python - <<'PY'
import os

from scripts.inbox.seats import SeatRegistry
from scripts.swarm.store import SwarmConfig, connect
from scripts.swarm_ledger.new_ledger import create

store, slug = connect(), os.environ["SLUG"]
create(slug, {"title": "Compose proof", "phases": [{"title": "Prove the compose stack"}]}, "swarm")
store.create(SwarmConfig(slug, "/data", 1, 1))
seats = SeatRegistry(store.redis)
seats.occupy(f"master@{slug}", "host-agent", 0)
seats.occupy(f"eng-1@{slug}", "member-agent", 0)
print(f"seeded swarm {slug} with host-agent and member-agent seated, in the compose Redis over TLS")
PY
docker compose restart controller >/dev/null
held=""
for _ in $(seq 30); do
  held="$(docker compose exec -T swarm agentihooks swarm "$slug" controller || true)"
  if [[ $held == *'"owner": "compose-controller"'* ]]; then
    break
  fi
  sleep 2
done
printf 'controller lease: %s\n' "$held"
if [[ $held != *'"owner": "compose-controller"'* ]]; then
  docker compose logs controller >&2
  printf 'the controller never took the lease\n' >&2
  exit 1
fi

docker run -d --name "$run-member" --network "${run}_default" -v "$tls/ca.pem:/tls/ca.pem:ro" \
  -e AGENTIHOOKS_DEPLOYMENT=compose -e SSL_CERT_FILE=/tls/ca.pem -e AGENTIHOOKS_AGENT_NAME=member-agent \
  ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev sleep infinity >/dev/null
code="$(docker compose exec -T hive agentihooks hive invite member)"
joined="$(docker exec "$run-member" agentihooks hive join https://hive:8770 "$code")"
printf 'member container: %s\n' "${joined%%;*}"
docker exec "$run-member" python -c '
from scripts.swarm.store import redis_client
client = redis_client()
assert client.acl_whoami().startswith("hive-"), client.acl_whoami()
print(f"member Redis user over TLS: {client.acl_whoami()}")
'

docker exec "$run-member" agentihooks msg send "master@$slug" "hello from the member hive"
inbox="$(docker compose exec -T -e AGENTIHOOKS_AGENT_NAME=host-agent swarm agentihooks msg inbox)"
printf 'host inbox: %s\n' "$inbox"
[[ $inbox == *"hello from the member hive"* ]]
docker compose exec -T -e AGENTIHOOKS_AGENT_NAME=host-agent swarm agentihooks msg send "eng-1@$slug" "hello from the host"
inbox="$(docker exec "$run-member" agentihooks msg inbox)"
printf 'member inbox: %s\n' "$inbox"
[[ $inbox == *"hello from the host"* ]]
sleep 5
state="$(docker inspect -f '{{.State.Running}} {{.RestartCount}} {{.State.Health.Status}}' "$(docker compose ps -q controller)")"
if [[ $state != "true 0 healthy" ]]; then
  docker compose logs controller 2>&1 | grep -v swarm_tick_step | tail -5 >&2
  printf 'the controller is not up after taking the lease (running, restarts, health: %s)\n' "$state" >&2
  exit 1
fi
printf 'controller: running, no restarts, healthy on its lease\n'
printf 'Compose hive smoke passed\n'
