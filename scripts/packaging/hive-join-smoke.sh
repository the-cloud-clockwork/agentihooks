#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
run="hive-proof-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
image="$run:local"
tls="$(mktemp -d)"
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
docker build -q -t "$image" . >/dev/null
docker network create "$run" >/dev/null
docker run -d --name "$run-redis" --network "$run" redis:7-alpine >/dev/null
docker run -d --name "$run-hive" --network "$run" --user "$(id -u):$(id -g)" -v "$tls:/tls:ro" \
  -e AGENTIHOOKS_SWARM_REDIS_URL="redis://$run-redis:6379/0" \
  "$image" "${hive[@]}" serve --host 0.0.0.0 --port 8770 --tls-cert /tls/cert.pem --tls-key /tls/key.pem >/dev/null
docker run -d --name "$run-member" --network "$run" -v "$tls/cert.pem:/tls/cert.pem:ro" \
  -e AGENTIHOOKS_DEPLOYMENT=compose -e SSL_CERT_FILE=/tls/cert.pem "$image" sleep infinity >/dev/null
ready=""
for _ in $(seq 60); do
  logs="$(docker logs "$run-hive" 2>&1)"
  if [[ $logs == *"hive join endpoint on https://"* ]]; then
    ready=1
    break
  fi
  sleep 1
done
if [[ -z $ready ]]; then
  printf 'the hive join endpoint never started:\n%s\n' "$logs" >&2
  exit 1
fi

if docker exec "$run-member" "${hive[@]}" join "http://$run-hive:8770" "unused" 2>/dev/null; then
  printf 'a plain http join off loopback was accepted\n' >&2
  exit 1
fi
printf 'plain http join off loopback: refused\n'
code="$(docker exec "$run-hive" "${hive[@]}" invite member)"
joined="$(docker exec "$run-member" "${hive[@]}" join "https://$run-hive:8770" "$code")"
printf 'member container: %s\n' "${joined%%;*}"
member_id="${joined#joined the hive as }"
member_id="${member_id%%;*}"

docker exec -i -e JOINED="$joined" "$run-member" python - <<'PY'
import os
import stat
from pathlib import Path

import redis

from scripts.swarm.keyspace import ROOT
from scripts.swarm.store import redis_client

env_file = Path.home() / ".agentihooks" / "hive.env"
env = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
assert env["AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL"] not in os.environ["JOINED"]
assert env["AGENTIHOOKS_HIVE_REDIS_URL"] not in os.environ["JOINED"]
print("hive.env: mode 0600, credentials absent from the join output")
client = redis_client()
assert client.acl_whoami() == f"hive-{env['AGENTIHOOKS_HIVE_ID']}"
assert client.set(f"{ROOT}:hive-proof", "1")
print(f"Redis as {client.acl_whoami()}: SET under {ROOT}: allowed")
for command in (("FLUSHALL",), ("CONFIG", "GET", "maxmemory"), ("SET", "outside", "1"), ("SET", f"{ROOT}-hive:invite:forged", "1"), ("ACL", "LIST")):
    try:
        client.execute_command(*command)
    except redis.exceptions.NoPermissionError as refused:
        print(f"{command[0]}: refused ({refused})")
    else:
        raise SystemExit(f"{command[0]} was allowed for a hive member")
PY

if docker exec "$run-member" "${hive[@]}" join "https://$run-hive:8770" "$code" 2>/dev/null; then
  printf 'a reused code joined\n' >&2
  exit 1
fi
printf 'reused code: refused\n'

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
