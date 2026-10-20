#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
run="hive-proof-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
image="$run:local"
tls="$(mktemp -d -p "${RUNNER_TEMP:-${TMPDIR:-/tmp}}")"
cleanup() {
  docker rm -f "$run-redis" "$run-hive" "$run-member" >/dev/null 2>&1 || true
  docker network rm "$run" >/dev/null 2>&1 || true
  docker image rm "$image" >/dev/null 2>&1 || true
  rm -r -- "$tls"
}
trap cleanup EXIT
hive=(python -c "import sys; from scripts.hive.cli import main; sys.exit(main(sys.argv[1:]))")

openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=$run-hive" -addext "subjectAltName=DNS:$run-hive" \
  -keyout "$tls/key.pem" -out "$tls/cert.pem" 2>/dev/null
chmod 0644 "$tls/cert.pem"
bash scripts/packaging/pull-runtime-image.sh mirror.gcr.io/library/redis:7-alpine
if [[ -n "${SWARM_SMOKE_IMAGE:-}" ]]; then
  docker tag "$SWARM_SMOKE_IMAGE" "$image"
else
  bash scripts/packaging/pull-runtime-image.sh mirror.gcr.io/library/python:3.12-slim-bookworm
  docker build --progress=plain -t "$image" .
fi
docker network create "$run" >/dev/null
docker run --pull never -d --name "$run-redis" --network "$run" mirror.gcr.io/library/redis:7-alpine >/dev/null
docker run -d --name "$run-hive" --network "$run" --user "$(id -u):$(id -g)" -v "$tls:/tls:ro" \
  -e AGENTIHOOKS_SWARM_REDIS_URL="redis://$run-redis:6379/0" \
  "$image" "${hive[@]}" serve --host 0.0.0.0 --port 8770 --tls-cert /tls/cert.pem --tls-key /tls/key.pem >/dev/null
docker run -d --name "$run-member" --network "$run" -v "$tls/cert.pem:/tls/cert.pem:ro" \
  -e AGENTIHOOKS_DEPLOYMENT=compose -e SSL_CERT_FILE=/tls/cert.pem "$image" sleep infinity >/dev/null
ready=""
for _ in $(seq 60); do
  logs="$(docker logs "$run-hive" 2>&1)"
  if [[ $logs == *"hive endpoint on https://"* ]]; then
    ready=1
    break
  fi
  sleep 1
done
if [[ -z $ready ]]; then
  printf 'the hive endpoint never started:\n%s\n' "$logs" >&2
  exit 1
fi

docker exec "$run-member" python -c '
import urllib.error, urllib.request

try:
    urllib.request.urlopen(urllib.request.Request("https://'"$run-hive"':8770/hive/join", data=b"{}"), timeout=5)
except urllib.error.HTTPError as exc:
    assert exc.code == 404, exc.code
    print("hive/join: 404")
else:
    raise SystemExit("/hive/join exists")
'
member_env="$(docker exec "$run-hive" python -c '
import os
from scripts.hive import auth
from scripts.swarm.store import redis_client, redis_url
g = auth.mint(redis_client(), "member", redis_url(os.environ))
print(g["id"])
print(auth.env_text("https://'"$run-hive"':8770", g), end="")
')"
member_id="$(head -n1 <<< "$member_env")"
tail -n +2 <<< "$member_env" | docker exec -i "$run-member" sh -c 'home="${AGENTIHOOKS_HOME:-$HOME/.agentihooks}"; umask 077; mkdir -p "$home"; cat > "$home/hive.env"'
unset member_env
printf 'member container: minted member %s\n' "$member_id"

docker exec -i "$run-member" python - <<'PY'
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import redis

from scripts.hive import daemon, orders, registry
from scripts.inbox.store import InboxStore
from scripts.swarm import idle
from scripts.swarm.keyspace import ROOT
from scripts.swarm.store import redis_client

env_file = Path.home() / ".agentihooks" / "hive.env"
env = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
print("hive.env: mode 0600")
client = redis_client()
member = env["AGENTIHOOKS_HIVE_ID"]
assert client.acl_whoami() == f"hive-{member}"
for command in (
    ("HSET", f"{ROOT}:swarm:proof:config", "max_eng", "99"),
    ("HSET", f"{ROOT}:swarm:proof:tasks", "forged", "{}"),
    ("HSET", f"{ROOT}:hive:other-member", "heartbeat_at", "1"),
    ("HSET", f"{ROOT}:hive:other-member:inbox:item:forged", "state", "done"),
    ("SET", f"{ROOT}:hive:other-member:swarm:proof:wait:other-agent", "{}"),
    ("HSET", f"{ROOT}:inbox:item:forged", "state", "done"),
    ("SET", f"{ROOT}:inbox:owner:master@proof", "forged"),
    ("SET", f"{ROOT}:swarm:proof:heartbeat:other-agent", "{}"),
    ("SET", f"{ROOT}:swarm:proof:stop-blocks:other-agent", "{}"),
    ("SREM", f"{ROOT}:hives", member),
    ("SADD", f"{ROOT}:hives", "forged"),
    ("PEXPIRE", f"{ROOT}:launch-slot:forged", "45000"),
    ("SET", f"{ROOT}:hive-proof", "1"),
    ("FLUSHALL",),
    ("CONFIG", "GET", "maxmemory"),
    ("SET", "outside", "1"),
    ("SET", f"{ROOT}-hive:invite:forged", "1"),
    ("ACL", "LIST"),
):
    try:
        client.execute_command(*command)
    except redis.exceptions.NoPermissionError as refused:
        print(f"{command[0]}: refused ({refused})")
    else:
        raise SystemExit(f"{command[0]} was allowed for a hive member")
client.hset(f"{ROOT}:swarm:proof:executions", "member-granted", "{}")
client.hdel(f"{ROOT}:swarm:proof:executions", "member-granted")
print("HSET swarm executions: allowed for a hive member")
os.environ.update(AGENTIHOOKS_HIVE_ID=member, AGENTIHOOKS_HIVE_REDIS_URL=env["AGENTIHOOKS_HIVE_REDIS_URL"])
assert daemon.hive_id() == member


class NoOrders:
    runtime = SimpleNamespace(herdr=None)

    def poll(self):
        return 0

    def poll_commands(self):
        return 0

    def sync_workspaces(self, ttl):
        return None


orders.Consumer.local = classmethod(lambda cls, redis, hive: NoOrders())
daemon.execution_report = lambda redis, local_id, herdr: {"names": [], "panes": {}, "executions": {}}
daemon.beat(client, member)
assert registry.live(registry.show(client, member), int(time.time() * 1000))
assert member in client.smembers(registry.INDEX)
idle.beat(client, "proof", "member-agent", "working", 1)
item = InboxStore(client).send("member-agent", "master@proof", "hello from the member")
assert [mail.id for mail in InboxStore(client).open_items("master@proof")] == [item.id]
assert client.exists(f"{ROOT}:hive:{member}:swarm:proof:heartbeat:member-agent")
assert client.exists(f"{ROOT}:hive:{member}:inbox:item:{item.id}")
assert client.hgetall(f"{ROOT}:swarm:proof:agents") == {}
print(f"Redis as {client.acl_whoami()}: own hive heartbeat, agent heartbeat, inbox send and agent record reads allowed")
PY
docker exec -i -e MEMBER_ID="$member_id" "$run-hive" python - <<'PY'
import os

from scripts.hive import auth
from scripts.inbox import relay
from scripts.inbox.store import InboxStore
from scripts.swarm.store import redis_client

client = redis_client()
member = os.environ["MEMBER_ID"]
assert client.exists(f"{auth.PREFIX}:member:{member}")
[sent] = InboxStore(client, world=member).pending_items("master@proof")
assert relay.relay(client) == []
assert InboxStore(client, world=member).get(sent.id).reason == relay.FOREIGN_SENDER
assert InboxStore(client).open_items("master@proof") == []
print("controller relay: a member send whose sender the member does not host is refused")
PY

docker exec "$run-hive" "${hive[@]}" revoke "$member_id"
docker exec -i "$run-member" python - <<'PY'
import redis

from scripts.swarm.store import redis_client

try:
    redis_client()
except redis.exceptions.AuthenticationError as refused:
    print(f"after revoke: Redis refused ({refused})")
else:
    raise SystemExit("the revoked Redis user still connects")
PY
docker exec -i -e MEMBER_ID="$member_id" "$run-hive" python - <<'PY'
import os

from scripts.hive import auth
from scripts.swarm.store import redis_client

client = redis_client()
assert client.keys(f"{auth.PREFIX}:member:*") == []
assert client.keys(f"{auth.PREFIX}:ledger:*") == []
assert f"hive-{os.environ['MEMBER_ID']}" not in client.acl_users()
print("after revoke: ledger credential and ACL user gone")
PY
printf 'Hive two-container join smoke passed\n'
