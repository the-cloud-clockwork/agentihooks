#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
run="compose-recovery-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
owned="label=com.docker.compose.project=$run"
export COMPOSE_PROJECT_NAME="$run" SWARM_PORT="${SWARM_PORT:-38765}" SWARM_REDIS_PORT="${SWARM_REDIS_PORT:-36379}"
SWARM_REDIS_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe())')"
export SWARM_REDIS_PASSWORD
nats_image=mirror.gcr.io/library/nats:2.15.1-alpine
backup="$(mktemp -d -p "${RUNNER_TEMP:-${TMPDIR:-/tmp}}")"
cleanup() {
  docker compose down --volumes >/dev/null 2>&1 || true
  rm -rf "$backup"
}
trap cleanup EXIT

bash scripts/packaging/pull-runtime-image.sh mirror.gcr.io/library/redis:7
bash scripts/packaging/pull-runtime-image.sh "$nats_image"
build=(--build)
if [[ -n "${SWARM_SMOKE_IMAGE:-}" ]]; then
  docker tag "$SWARM_SMOKE_IMAGE" ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev
  build=(--no-build)
else
  bash scripts/packaging/pull-runtime-image.sh ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev
fi
up() {
  if ! docker compose up --pull never "${build[@]}" --wait --wait-timeout 180 "$@"; then
    docker compose logs >&2
    exit 1
  fi
}
up redis nats swarm
printf 'stack up: redis, nats and swarm healthy in project %s\n' "$run"

export SLUG="recovery-drill" LEDGER_HOST="127.0.0.1"
DRILL="$(cat <<'PY'
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from types import SimpleNamespace

from scripts.events.config import BusConfig
from scripts.events.factory import open_bus
from scripts.hive import execution_events, registry, reports
from scripts.hive.spoke import Spoke, SpokeConfig
from scripts.swarm import freeze, readiness
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import AgentRecord, SwarmConfig, connect
from scripts.swarm_ledger.new_ledger import create
from scripts.swarm_ledger.repository import hierarchy
from scripts.swarm_v2.runtime.base import Outcome, SpawnRequest, Status

TASKS = ("t1", "t2", "t3", "t4", "t5")
HIVE = "recovery-hive"
LAUNCHES = "/data/recovery-launches.jsonl"
store, slug, ledger = connect(), os.environ["SLUG"], LedgerClient(service=True)


def view():
    config, doc = store.config(slug), ledger.state(slug)
    return readiness.Snapshot(
        slug,
        {task["id"]: task for task in doc["tasks"]},
        doc,
        hierarchy.project(doc)[0],
        freeze.fix_phase(config),
        lambda task_id: store.claimant(slug, task_id) is not None,
        lambda task: True,
    )


def seed(offered):
    tasks = [{"title": f"Drill task {n}", "phase": "p1", "lane": "eng"} for n in range(1, len(TASKS) + 1)]
    create(slug, {"title": "Recovery drill", "phases": [{"title": "Survive a JetStream restore"}], "tasks": tasks}, "swarm")
    store.create(SwarmConfig(slug, "/data", len(TASKS), 1))
    for task in TASKS:
        ledger.update_task(slug, task, {"kind": "ops"})
    current = view()
    current = replace(current, rows={task: current.rows[task] for task in offered})
    bus = open_bus(BusConfig.from_environ())
    placed = readiness.publish(bus, current, int(time.time() * 1000))
    bus.close()
    assert placed == [f"ready:{slug}:{task}:0" for task in offered], placed
    return placed


class Tasks:
    def request(self, event):
        name = store.next_name(slug, "eng")
        at = int(time.time() * 1000)
        assert store.claim(slug, event.key, name, 600_000)
        seat = f"eng-{len(launched()) + 1}@{slug}"
        agent = store.start_execution(
            slug, AgentRecord(name, "eng", event.key, seat=seat, started_at=at, hive=HIVE, runtime_target={"host_id": HIVE})
        )
        store.seats.occupy(agent.seat, name, at)
        ledger.update_task(slug, event.key, {"state": "claimed", "claimed_by": name})
        generation = ledger.task(slug, event.key)["claim_generation"]
        with open(LAUNCHES, "a") as launches:
            launches.write(json.dumps({"task": event.key, "name": name}) + "\n")
        task = {"id": event.key, "execution_id": agent.execution_id,
                "execution_generation": agent.generation, "claim_generation": generation}
        return SpawnRequest(store.config(slug), "eng", name, task)

    def reopen(self, event):
        return None


class StandIn:
    def spawn(self, request):
        return Outcome("spawn", Status.OK, "recovery", SimpleNamespace(pane_id=f"pane-{request.name}", harness="claude", account=""))


def launched():
    if not os.path.exists(LAUNCHES):
        return []
    with open(LAUNCHES) as launches:
        return [json.loads(line)["task"] for line in launches if line.strip()]


def take(count):
    before = len(launched())
    bus = open_bus(BusConfig.from_environ())
    spoke = Spoke(SpokeConfig(store.config(slug), "eng", HIVE), bus, StandIn(), Tasks(),
                  measure=lambda: {"cpus": 8, "load5": 0.0}, view=view)
    outcomes, empty = [], 0
    while len(launched()) - before < count and empty < 5:
        outcomes.append(spoke.poll(1_000))
        empty = empty + 1 if outcomes[-1] == "empty" else 0
    bus.close()
    return {"took": launched()[before:], "outcomes": outcomes}


def exit_report(agents):
    executions = {agent.execution_id: {"slug": slug, "name": agent.name, "generation": agent.generation,
                                       "pane": {"state": "exited"}} for agent in agents}
    live = [agent.pane_id for agent in store.agents(slug) if agent.state != "finished" and agent not in agents]
    report = {"observed_at": int(time.time() * 1000), "pane_ids": live, "executions": executions}
    store.redis.sadd(registry.INDEX, HIVE)
    store.redis.set(reports.key(HIVE), json.dumps(report))
    bus = open_bus(BusConfig.from_environ())
    execution_events.publish(bus, HIVE, report)
    bus.close()
    return sorted(agent.task for agent in agents)


def done(tasks):
    agents = [agent for agent in store.agents(slug) if agent.task in tasks and agent.state != "finished"]
    for agent in agents:
        subprocess.run(
            ["agentihooks", "swarm", slug, "done", "--command", "recovery drill", "--output", f"{agent.task} finished"],
            env={**os.environ, "AGENTIHOOKS_AGENT_NAME": agent.name}, check=True, stdout=sys.stderr,
        )
    return exit_report(agents)


def die(tasks):
    return exit_report([agent for agent in store.agents(slug) if agent.task in tasks and agent.state != "finished"])


def state():
    rows = {task["id"]: task for task in ledger.state(slug)["tasks"]}
    counts = launched()
    return {
        "tasks": {task: {"ledger": rows[task]["state"], "claim": store.claimant(slug, task),
                         "launches": counts.count(task)} for task in TASKS},
        "agents": sorted(agent.task for agent in store.agents(slug) if agent.state != "finished"),
    }


step, args = sys.argv[1], sys.argv[2:]
result = {"seed": lambda: seed(args), "take": lambda: take(int(args[0])), "done": lambda: done(args),
          "die": lambda: die(args),
          "state": state}[step]()
print(json.dumps(result))
PY
)"
drill() {
  docker compose exec -T -e SLUG -e LEDGER_HOST swarm python - "$@" <<<"$DRILL"
}
hub() {
  docker compose exec -T -e LEDGER_HOST swarm agentihooks hub run --once >/dev/null
}
volume() {
  docker run --rm --entrypoint sh -v "${run}_nats-data:/data" -v "$backup:/backup" "$nats_image" -c "$1"
}

settle() {
  for _ in $(seq 30); do
    hub
    found="$(drill state)"
    local missing=0 want
    for want in "$@"; do
      [[ $found == *"$want"* ]] || missing=1
    done
    ((missing)) || return 0
    sleep 1
  done
  printf 'the hub never reached %s; the ledger against the recorded state: %s\n' "$*" "$found" >&2
  docker compose logs swarm >&2
  exit 1
}

printf 'offered before the backup: %s\n' "$(drill seed t1 t2 t3 t4)"
printf 'hive took before the backup: %s\n' "$(drill take 2)"

docker compose stop nats >/dev/null
volume 'tar -C /data -cf /backup/jetstream.tar .'
up nats
printf 'JetStream backed up cold from the stopped NATS volume: %s bytes\n' "$(stat -c %s "$backup/jetstream.tar")"

printf 'done after the backup: %s\n' "$(drill done t1)"
settle '"t1": {"ledger": "done"'
printf 'hive took after the backup: %s\n' "$(drill take 1)"
printf 'state before the restore: %s\n' "$(drill state)"

docker compose stop nats >/dev/null
volume 'find /data -mindepth 1 -delete && tar -C /data -xf /backup/jetstream.tar'
up nats
printf 'JetStream restored from the backup, losing every write after it\n'

hub
printf 'hive took after the restore: %s\n' "$(drill take 2)"
printf 'agents died after the restore: %s\n' "$(drill die t2 t3)"
settle '"t2": {"ledger": "open"' '"t3": {"ledger": "open"'
printf 'hive relaunched the reopened tasks: %s\n' "$(drill take 2)"
printf 'done after the restore: %s\n' "$(drill done t2 t3 t4 t5)"
want='{"tasks": {"t1": {"ledger": "done", "claim": null, "launches": 1}, "t2": {"ledger": "done", "claim": null, "launches": 2}, '
want+='"t3": {"ledger": "done", "claim": null, "launches": 2}, "t4": {"ledger": "done", "claim": null, "launches": 1}, '
want+='"t5": {"ledger": "done", "claim": null, "launches": 1}}, "agents": []}'
settle "$want"
printf 'ledger against the recorded state after reconcile: %s\n' "$found"

cleanup
left="$(docker ps --all --quiet --filter "$owned")$(docker volume ls --quiet --filter "$owned")"
left+="$(docker network ls --quiet --filter "$owned")"
if [[ -n "$left" ]]; then
  printf 'the stack left containers, volumes or networks behind: %s\n' "$left" >&2
  exit 1
fi
printf 'stack removed with its volumes and network\n'
printf 'Recovery drill passed: after a JetStream restore the hub reconciled every task to done, each launched once\n'
