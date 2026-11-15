#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
run="stability-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
tls="$(mktemp -d -p "${RUNNER_TEMP:-${TMPDIR:-/tmp}}")"
override="$tls/compose.override.yaml"
export COMPOSE_PROJECT_NAME="$run" COMPOSE_PROFILES=join SWARM_TLS_DIR="$tls" SWARM_REDIS_SCHEME=rediss \
  SWARM_CA_FILE=/tls/ca.pem SWARM_MEMBER_REDIS_URL=rediss://redis:6379/0 SWARM_PORT="${SWARM_PORT:-48765}" \
  SWARM_HIVE_PORT="${SWARM_HIVE_PORT:-48770}" SWARM_REDIS_PORT="${SWARM_REDIS_PORT:-46379}"
SWARM_REDIS_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe())')"
export SWARM_REDIS_PASSWORD
compose=(docker compose -f compose.yaml -f "$override")
cleanup() {
  docker rm -f "$run-member" >/dev/null 2>&1 || true
  "${compose[@]}" down --volumes >/dev/null 2>&1 || true
  rm -r -- "$tls"
}
trap cleanup EXIT

openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=$run" \
  -addext "subjectAltName=DNS:redis,DNS:hive,DNS:swarm,IP:127.0.0.1" \
  -keyout "$tls/key.pem" -out "$tls/cert.pem" 2>/dev/null
cp "$tls/cert.pem" "$tls/ca.pem"
cat >"$override" <<'YAML'
services:
  swarm:
    environment:
      LEDGER_TLS_CERT: /tls/cert.pem
      LEDGER_TLS_KEY: /tls/key.pem
    healthcheck:
      test: [CMD, python, -c, "import urllib.request; urllib.request.urlopen('https://127.0.0.1:8765/healthz', timeout=2).read()"]
YAML
chmod 0755 "$tls"
chmod 0644 "$tls"/*.pem "$override"
bash scripts/packaging/pull-runtime-image.sh mirror.gcr.io/library/redis:7
bash scripts/packaging/pull-runtime-image.sh mirror.gcr.io/library/nats:2.15.1-alpine
build=(--build)
if [[ -n "${SWARM_SMOKE_IMAGE:-}" ]]; then
  docker tag "$SWARM_SMOKE_IMAGE" ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev
  build=(--no-build)
else
  bash scripts/packaging/pull-runtime-image.sh ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev
fi
if ! "${compose[@]}" up --pull never "${build[@]}" --wait --wait-timeout 180; then
  "${compose[@]}" logs >&2
  exit 1
fi
hub=("${compose[@]}" exec -T -e LEDGER_HOST=127.0.0.1)

slug="stability-proof"
"${hub[@]}" -e SLUG="$slug" swarm python - <<'PY'
import os

from scripts.swarm.store import SwarmConfig, connect
from scripts.swarm_ledger.new_ledger import create

store, slug = connect(), os.environ["SLUG"]
create(slug, {"title": "Stability proof", "phases": [{"title": "Hold every stability invariant"}]}, "swarm")
store.create(SwarmConfig(slug, "/data", 2, 0, state="paused"))
print(f"created swarm {slug} with its ledger")
PY

docker run -d --name "$run-member" --network "${run}_default" -v "$tls/ca.pem:/tls/ca.pem:ro" \
  -e AGENTIHOOKS_DEPLOYMENT=compose -e SSL_CERT_FILE=/tls/ca.pem -e LEDGER_URL=https://swarm:8765 \
  -e AGENTIHOOKS_SWARM="$slug" \
  ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev sleep infinity >/dev/null
grant="$("${compose[@]}" exec -T -e SWARM_MEMBER_REDIS_URL="$SWARM_MEMBER_REDIS_URL" hive python -c '
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
printf 'joined member hive %s\n' "$member_id"
docker exec -d "$run-member" sh -c 'agentihooks hive run >"$HOME/hive-run.log" 2>&1'

agents="$("${hub[@]}" -e SLUG="$slug" -e MEMBER_ID="$member_id" swarm python - <<'PY'
import os
import time

from scripts.swarm.store import AgentRecord, connect

store, slug, member = connect(), os.environ["SLUG"], os.environ["MEMBER_ID"]
for seat in (1, 2):
    name = store.next_name(slug, "eng")
    started = int(time.time() * 1000)
    agent = AgentRecord(
        name, "eng", f"task-{seat}", seat=f"eng-{seat}@{slug}", started_at=started, hive=member,
        runtime_target={"host_id": member}, harness="claude",
    )
    store.start_execution(slug, agent)
    store.seats.occupy(agent.seat, name, started)
    print(name)
PY
)"
mapfile -t placed <<< "$agents"
printf 'launched %s on hive %s\n' "${placed[*]}" "$member_id"
for agent in "${placed[@]}"; do
  docker exec -e AGENTIHOOKS_AGENT_NAME="$agent" "$run-member" sh -c \
    'set -a; . "${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/hive.env"; set +a; exec python -c "$0"' \
    'import os; from hooks.context import hive_join; hive_join.run({"session_id": os.environ["AGENTIHOOKS_AGENT_NAME"]}, "start")'
done
"${hub[@]}" -e SLUG="$slug" swarm python - <<'PY'
import json
import os

from scripts.hive import join_report
from scripts.swarm.store import connect

store, slug = connect(), os.environ["SLUG"]
for agent in store.agents(slug):
    report = join_report.read(store.redis, agent)
    row = {"agent": agent.name, "task": agent.task, "at": agent.started_at, "state": "started" if report else "failed"}
    row |= {"error": "" if report else "no hive join report", "pane_id": agent.pane_id, "after": "", "head": ""}
    store.redis.hset(store.key(slug, "launches"), agent.name, json.dumps(row))
    print(f"launch {'started' if report else 'failed'} for {agent.name}: " + (join_report.summary(report) if report else "no hive join report"))
PY

live=""
live_row="$member_id"$'\t'live$'\t'
for _ in $(seq 60); do
  live="$(docker exec "$run-member" agentihooks hive list 2>/dev/null || true)"
  if [[ $live == *"$live_row"* ]]; then
    break
  fi
  sleep 1
done
printf 'hive list: %s\n' "$live"
if [[ $live != *"$live_row"* ]]; then
  docker exec "$run-member" sh -c 'tail -n 40 "$HOME/hive-run.log"' >&2 || true
  printf 'member hive %s never reported live\n' "$member_id" >&2
  exit 1
fi

graded=0
"${hub[@]}" -e SLUG="$slug" -e PLACED="${#placed[@]}" -e MEMBER_ID="$member_id" swarm python - <<'PY' || graded=$?
import os
import sys
import time

from scripts.swarm import probe
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import connect

GRADED = ("every hive live", "every hive agent joins and writes the ledger", "full task list reads per minute")
HOST_ONLY = ("workstation agents at or under the root cap", "merge queue groups moving", "release age")
slug, placed, member = os.environ["SLUG"], int(os.environ["PLACED"]), os.environ["MEMBER_ID"]
found = probe.run(connect(), slug, LedgerClient(), int(time.time() * 1000))
for result in found:
    print(f"skipped\t{result.name}\treads the root hive or GitHub" if result.name in HOST_ONLY else result.line())
graded = [result for result in found if result.name in GRADED or result.name.startswith("launch success hive")]
missing = [name for name in GRADED if name not in {result.name for result in graded}]
launched = [result for result in graded if result.name.startswith("launch success hive")]
joined = next((result for result in graded if result.name == GRADED[1]), None)
failed = [result.name for result in graded if not result.passed]
launch = f"launch success hive {member} harness claude"
if [result.name for result in launched] != [launch] or f"{placed} of {placed} started" not in launched[0].measured:
    failed.append(f"launch rows {[result.line() for result in launched]}, expected one {launch} with {placed} started")
if missing or joined is None or not joined.measured.startswith(f"{placed} of {placed} "):
    failed.append(f"probe coverage: missing {missing}, join {joined}")
if failed:
    sys.exit(f"stability invariants failed: {failed}")
print(f"every stability invariant held on a swarm with one hive and {placed} agents")
PY
if [[ $graded != 0 ]]; then
  docker exec "$run-member" sh -c 'tail -n 40 "$HOME/hive-run.log"' >&2 || true
  "${compose[@]}" logs --tail 60 swarm controller >&2 || true
  exit 1
fi
printf 'Stability smoke passed\n'
