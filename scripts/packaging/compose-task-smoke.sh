#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
run="compose-task-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
owned="label=com.docker.compose.project=$run"
export COMPOSE_PROJECT_NAME="$run" SWARM_PORT="${SWARM_PORT:-28765}" SWARM_REDIS_PORT="${SWARM_REDIS_PORT:-26379}"
SWARM_REDIS_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe())')"
export SWARM_REDIS_PASSWORD
cleanup() {
  docker compose down --volumes >/dev/null 2>&1 || true
}
trap cleanup EXIT

bash scripts/packaging/pull-runtime-image.sh mirror.gcr.io/library/redis:7
bash scripts/packaging/pull-runtime-image.sh mirror.gcr.io/library/nats:2.15.1-alpine
build=(--build)
if [[ -n "${SWARM_SMOKE_IMAGE:-}" ]]; then
  docker tag "$SWARM_SMOKE_IMAGE" ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev
  build=(--no-build)
else
  bash scripts/packaging/pull-runtime-image.sh ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev
fi
if ! docker compose up --pull never "${build[@]}" --wait --wait-timeout 180 redis nats swarm; then
  docker compose logs >&2
  exit 1
fi
printf 'stack up: redis, nats and swarm healthy in project %s\n' "$run"

export SLUG="compose-task" LEDGER_HOST="127.0.0.1"
started="$(docker compose exec -T -e SLUG -e LEDGER_HOST swarm python - <<'PY'
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

from scripts.events.config import BusConfig
from scripts.events.factory import open_bus
from scripts.hive import admission, execution_events
from scripts.hive.spoke import Spoke, SpokeConfig
from scripts.swarm import freeze, readiness
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import AgentRecord, SwarmConfig, connect
from scripts.swarm_ledger.new_ledger import create
from scripts.swarm_ledger.repository import hierarchy
from scripts.swarm_v2.runtime.base import Outcome, SpawnRequest, Status

store, slug, hive = connect(), os.environ["SLUG"], "compose-task-hive"
create(slug, {"title": "Compose task proof", "phases": [{"title": "Run one task to done"}],
              "tasks": [{"title": "Report the stack is up", "phase": "p1", "lane": "eng"}]}, "swarm")
store.create(SwarmConfig(slug, "/data", 1, 1))
ledger = LedgerClient(service=True)
ledger.update_task(slug, "t1", {"kind": "ops"})
bus = open_bus(BusConfig.from_environ())
config = store.config(slug)
doc = ledger.state(slug)
view = readiness.Snapshot(
    slug,
    {task["id"]: task for task in doc["tasks"]},
    doc,
    hierarchy.project(doc)[0],
    freeze.fix_phase(config),
    lambda task_id: store.claimant(slug, task_id) is not None,
    lambda task: True,
)
offered = readiness.publish(bus, view, int(time.time() * 1000))
assert offered == [f"ready:{slug}:t1:0"], offered
print(f"offered on NATS: {offered}", flush=True, file=sys.stderr)
admitted = {}


class Tasks:
    def request(self, event):
        name = store.next_name(slug, "eng")
        at = int(time.time() * 1000)
        assert store.claim(slug, event.key, name, 600_000)
        agent = store.start_execution(slug, AgentRecord(name, "eng", event.key, seat=f"eng-1@{slug}", started_at=at, hive=hive))
        store.seats.occupy(agent.seat, name, at)
        ledger.update_task(slug, event.key, {"state": "claimed", "claimed_by": name})
        admitted.update(name=name, execution=agent.execution_id, generation=agent.generation)
        task = {"id": event.key, "execution_id": agent.execution_id,
                "execution_generation": agent.generation, "claim_generation": event.version}
        return SpawnRequest(config, "eng", name, task)

    def reopen(self, event):
        return None


class StandIn:
    def spawn(self, request):
        subprocess.run(
            ["agentihooks", "swarm", slug, "done", "--command", "docker compose up redis nats swarm",
             "--output", "redis, nats and swarm healthy"],
            env={**os.environ, "AGENTIHOOKS_AGENT_NAME": request.name}, check=True, stdout=sys.stderr,
        )
        return Outcome("spawn", Status.OK, "compose-task", SimpleNamespace(pane_id="stand-in", harness="claude", account=""))


probe = Spoke(SpokeConfig(config, "eng", hive), bus, StandIn(), Tasks())
for _ in range(2):
    reading = probe.measure()
print(f"runner headroom for a real agent: {admission.room(reading)}", file=sys.stderr)
stand_in = {"cpus": 1, "load5": 0.0}
spoke = Spoke(SpokeConfig(config, "eng", hive), bus, StandIn(), Tasks(), measure=lambda: stand_in)
outcomes = []
for _ in range(30):
    outcomes.append(spoke.poll(1_000))
    if outcomes[-1] == "started":
        break
    time.sleep(1)
assert outcomes[-1] == "started", f"the spoke never started the task: {outcomes}"
print(f"spoke took the offer from NATS: {outcomes}", file=sys.stderr)
report = {
    "observed_at": int(time.time() * 1000),
    "executions": {admitted["execution"]: {"slug": slug, "name": admitted["name"],
                                           "generation": admitted["generation"], "pane": {"state": "exited"}}},
}
execution_events.publish(bus, hive, report)
bus.close()
print(json.dumps(admitted))
PY
)"
printf 'stand in agent finished and its exit is on NATS: %s\n' "$started"

settled=""
for _ in $(seq 30); do
  docker compose exec -T -e LEDGER_HOST swarm agentihooks hub run --once
  settled="$(docker compose exec -T -e SLUG -e LEDGER_HOST -e STARTED="$started" swarm python - <<'PY'
import json
import os
from types import SimpleNamespace

from scripts.events.config import BusConfig
from scripts.events.factory import open_bus
from scripts.hive.spoke import claim_key
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import connect

slug, started = os.environ["SLUG"], json.loads(os.environ["STARTED"])
store, bus = connect(), open_bus(BusConfig.from_environ())
claim = bus.state("spoke-work").get(claim_key(SimpleNamespace(swarm=slug, key="t1", version=0)))
bus.close()
row = LedgerClient(service=True).task(slug, "t1")
print(json.dumps({
    "task": row["state"],
    "claim": store.claimant(slug, "t1"),
    "execution": store.execution(slug, started["execution"]).state,
    "agent": any(agent.name == started["name"] for agent in store.agents(slug)),
    "spoke": json.loads(claim.value)["state"] if claim else None,
}))
PY
)"
  [[ $settled == *'"spoke": "exited"'* ]] && break
  sleep 1
done
printf 'after the hub lifecycle pass: %s\n' "$settled"
if [[ $settled != '{"task": "done", "claim": null, "execution": "finished", "agent": false, "spoke": "exited"}' ]]; then
  docker compose logs swarm >&2
  printf 'the task did not settle to done through the hub\n' >&2
  exit 1
fi

cleanup
left="$(docker ps --all --quiet --filter "$owned")$(docker volume ls --quiet --filter "$owned")"
if [[ -n "$left" ]]; then
  printf 'the stack left containers or volumes behind: %s\n' "$left" >&2
  exit 1
fi
printf 'stack removed with its volumes\n'
printf 'Compose task smoke passed: one task offered on NATS, taken, done and settled by the hub\n'
