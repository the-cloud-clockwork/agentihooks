#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
run="hive-compose-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
tls="$(mktemp -d -p "${RUNNER_TEMP:-${TMPDIR:-/tmp}}")"
export COMPOSE_PROJECT_NAME="$run" COMPOSE_PROFILES=join SWARM_TLS_DIR="$tls" SWARM_REDIS_SCHEME=rediss \
  SWARM_CA_FILE=/tls/ca.pem SWARM_MEMBER_REDIS_URL=rediss://redis:6379/0 SWARM_PORT="${SWARM_PORT:-18765}" \
  SWARM_HIVE_PORT="${SWARM_HIVE_PORT:-18770}" SWARM_REDIS_PORT="${SWARM_REDIS_PORT:-16379}"
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
bash scripts/packaging/pull-runtime-image.sh mirror.gcr.io/library/redis:7
bash scripts/packaging/pull-runtime-image.sh mirror.gcr.io/library/nats:2.15.1-alpine
build=(--build)
if [[ -n "${SWARM_SMOKE_IMAGE:-}" ]]; then
  docker tag "$SWARM_SMOKE_IMAGE" ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev
  build=(--no-build)
else
  bash scripts/packaging/pull-runtime-image.sh ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev
fi
if ! docker compose up --pull never "${build[@]}" --wait --wait-timeout 180; then
  docker compose logs >&2
  exit 1
fi

slug="compose-proof"
host_agent="$(docker compose exec -T -e SLUG="$slug" swarm python - <<'PY'
import os
import time

from scripts.inbox.seats import SeatRegistry
from scripts.swarm.store import AgentRecord, SwarmConfig, connect
from scripts.swarm_ledger.new_ledger import create

store, slug = connect(), os.environ["SLUG"]
create(slug, {"title": "Compose proof", "phases": [{"title": "Prove the compose stack"}]}, "swarm")
store.create(SwarmConfig(slug, "/data", 1, 1))
seats = SeatRegistry(store.redis)
name = store.next_name(slug, "master")
started = int(time.time() * 1000)
store.start_execution(slug, AgentRecord(name, "master", "", seat=f"master@{slug}", started_at=started))
seats.occupy(f"master@{slug}", name, started)
print(name)
PY
)"
printf 'seeded swarm %s with %s seated, in the compose Redis over TLS\n' "$slug" "$host_agent"
docker compose restart controller >/dev/null
held=""
for _ in $(seq 60); do
  held="$(docker compose exec -T swarm agentihooks swarm "$slug" controller || true)"
  if [[ $held == *'"owner": "compose-controller"'* ]]; then
    break
  fi
  sleep 1
done
printf 'controller lease: %s\n' "$held"
if [[ $held != *'"owner": "compose-controller"'* ]]; then
  docker compose logs controller >&2
  printf 'the controller never took the lease\n' >&2
  exit 1
fi

docker run -d --name "$run-member" --network "${run}_default" -v "$tls/ca.pem:/tls/ca.pem:ro" \
  -e AGENTIHOOKS_DEPLOYMENT=compose -e SSL_CERT_FILE=/tls/ca.pem -e LEDGER_URL=http://swarm:8765 \
  -e AGENTIHOOKS_SWARM="$slug" \
  ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev sleep infinity >/dev/null
grant="$(docker compose exec -T -e SWARM_MEMBER_REDIS_URL="$SWARM_MEMBER_REDIS_URL" hive python -c '
import os
from scripts.hive import auth
from scripts.swarm.store import redis_client
g = auth.mint(redis_client(), "member", os.environ["SWARM_MEMBER_REDIS_URL"])
print(g["id"])
print(auth.env_text("https://hive:8770", g), end="")
')"
member_id="$(head -n1 <<< "$grant")"
tail -n +2 <<< "$grant" | docker exec -i "$run-member" sh -c 'home="${AGENTIHOOKS_HOME:-$HOME/.agentihooks}"; umask 077; mkdir -p "$home"; cat > "$home/hive.env"'
unset grant
printf 'member container: minted member %s\n' "$member_id"
docker exec "$run-member" python -c '
from scripts.swarm.store import redis_client
client = redis_client()
assert client.acl_whoami().startswith("hive-"), client.acl_whoami()
print(f"member Redis user over TLS: {client.acl_whoami()}")
'
member_agent="$(docker compose exec -T -e SLUG="$slug" -e MEMBER_ID="$member_id" swarm python -c '
import os, time
from scripts.inbox import exits
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord, connect
store, slug, member = connect(), os.environ["SLUG"], os.environ["MEMBER_ID"]
name = store.next_name(slug, "eng")
started = int(time.time() * 1000)
agent = AgentRecord(name, "eng", "task", seat=f"eng-1@{slug}", started_at=started, hive=member, runtime_target={"host_id": member})
def place_member():
    store.start_execution(slug, agent)
    store.seats.occupy(agent.seat, name, started)
    return {}
exits.sweep(InboxStore(store.redis), slug, store, place_member)
assert store.seats.exit_of(name) == {}, "the exit sweep marked the newly placed member as exited"
print(name)
')"
printf 'placed %s on the member in seat eng-1\n' "$member_agent"

docker exec -e AGENTIHOOKS_AGENT_NAME="$member_agent" "$run-member" agentihooks msg send "master@$slug" "hello from the member hive"
relayed="$(docker compose exec -T swarm python -c '
from scripts.inbox import relay
from scripts.swarm.store import connect
print(relay.relay(connect().redis))
')"
printf 'controller relay of member mail: %s\n' "$relayed"
inbox="$(docker compose exec -T -e AGENTIHOOKS_AGENT_NAME="$host_agent" swarm agentihooks msg inbox)"
printf 'host inbox: %s\n' "$inbox"
[[ $inbox == *"hello from the member hive"* ]]
docker compose exec -T -e AGENTIHOOKS_AGENT_NAME="$host_agent" swarm agentihooks msg send "eng-1@$slug" "hello from the host"
inbox="$(docker exec -e AGENTIHOOKS_AGENT_NAME="$member_agent" "$run-member" agentihooks msg inbox)"
printf 'member inbox: %s\n' "$inbox"
[[ $inbox == *"hello from the host"* ]]
for _ in $(seq 30); do
  state="$(docker inspect -f '{{.State.Running}} {{.RestartCount}} {{.State.Health.Status}}' "$(docker compose ps -q controller)")"
  if [[ $state != *starting ]]; then
    break
  fi
  sleep 2
done
if [[ $state != "true 0 healthy" ]]; then
  docker compose logs controller 2>&1 | grep -v swarm_tick_step | tail -5 >&2
  printf 'the controller is not up after taking the lease (running, restarts, health: %s)\n' "$state" >&2
  exit 1
fi
printf 'controller: running, no restarts, healthy on its lease\n'

member_auth='
import os, hooks.config, redis
client = redis.Redis.from_url(os.environ["AGENTIHOOKS_HIVE_REDIS_URL"], decode_responses=True)
print(f"member Redis user from hive.env: {client.acl_whoami()}")
'
docker exec "$run-member" agentihooks hive refresh >/dev/null
printf 'after a refresh through the join service, '
docker exec "$run-member" python -c "$member_auth"
docker compose restart redis >/dev/null
for _ in $(seq 30); do
  docker exec "$run-member" python -c "$member_auth" >/dev/null 2>&1 && break
  sleep 1
done
printf 'after a Redis restart, the refreshed '
docker exec "$run-member" python -c "$member_auth"
docker exec "$run-member" agentihooks hive refresh >/dev/null
printf 'after a Redis restart, a refresh still finds the member record\n'
docker compose exec -T -e SLUG="$slug" -e MEMBER_ID="$member_id" -e MEMBER_AGENT="$member_agent" swarm python -c '
import os
from scripts.hive import registry
from scripts.inbox.seats import SeatRegistry
from scripts.swarm.store import connect
slug = os.environ["SLUG"]
redis = connect().redis
seat = SeatRegistry(redis).occupant(f"eng-1@{slug}")
assert seat and seat.occupant == os.environ["MEMBER_AGENT"], f"the seat lost its occupant after the Redis restart: {seat}"
print(f"after a Redis restart, the seat is still held by {seat.occupant}")
assert os.environ["MEMBER_ID"] in redis.smembers(registry.INDEX), "the member left the hive index"
print("after a Redis restart and a refresh, the member is still in the hive index")
'
anonymous="$(docker compose exec -T redis redis-cli --tls --cacert /run/swarm-tls/ca.pem ping 2>&1 || true)"
printf 'unauthenticated Redis client: %s\n' "$anonymous"
[[ $anonymous == *NOAUTH* ]]
printf 'Compose hive smoke passed\n'
