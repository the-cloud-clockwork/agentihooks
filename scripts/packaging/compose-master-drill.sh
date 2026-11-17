#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
run="compose-master-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
owned="label=com.docker.compose.project=$run"
export COMPOSE_PROJECT_NAME="$run" SWARM_PORT="${SWARM_PORT:-22765}" SWARM_REDIS_PORT="${SWARM_REDIS_PORT:-22379}"
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

export SLUG="proof-ad0e11-master-1" LEDGER_HOST="127.0.0.1"
DRILL="$(cat <<'PY'
import json
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from scripts.swarm import master_drill
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import MASTER, AgentRecord, SwarmConfig, connect
from scripts.swarm.tick import Placed
from scripts.swarm_ledger.new_ledger import create
from scripts.swarm_v2 import front_door
from scripts.tick.steps import masters

OLD = "master@ad0e11-0001"
store, slug, ledger = connect(), os.environ["SLUG"], LedgerClient(service=True)


class StandIn:
    def __init__(self) -> None:
        self.live: set[str] = {OLD}
        self.launched: list[str] = []

    def live_names(self) -> set[str]:
        return set(self.live)

    def has_capacity(self, config: SwarmConfig) -> bool:
        return True

    def spawn(self, config: SwarmConfig, lane: str, name: str, task: dict[str, Any]) -> Placed:
        self.live.add(name)
        self.launched.append(name)
        return Placed(pane_id=f"pane-{name}", harness="claude", account="login", route_kind="master-login")

    def reported(self, agent: AgentRecord) -> bool:
        return agent.name in self.live

    def retire(self, agent: AgentRecord, homes: Sequence[Path] = ()) -> bool:
        self.live.discard(agent.name)
        return True


create(slug, {"title": "Master drill", "phases": [{"title": "Force a master into an empty front door seat"}], "tasks": []}, "swarm")
store.create(SwarmConfig(slug, "/data", max_eng=0, max_ci=0, max_plan=0, scratch=True))
store.update(slug, state="running")
now = int(time.time() * 1000)
seat = front_door.seat(slug)
store.put_agent(slug, AgentRecord(OLD, MASTER, MASTER, started_at=now - 3_600_000, state="working", seat=seat,
                                  pane_id="pane-old", harness="claude", route_kind="master-login"))
runtime = StandIn()
drill = master_drill.Drill(store, slug, ledger, runtime, {"tasks": ledger.tasks(slug)}, now, masters._night_watch)
record = master_drill.run(drill)
print(json.dumps({**record, "launched": runtime.launched}))
PY
)"
record="$(docker compose exec -T -e SLUG -e LEDGER_HOST swarm python - <<<"$DRILL")"
printf 'master drill record: %s\n' "$record"
python3 - "$record" <<'PY'
import json
import sys

record = json.loads(sys.argv[1])
assert record["retired"] == ["master@ad0e11-0001"], record["retired"]
assert record["successor"] in record["launched"] and record["successor"] != "master@ad0e11-0001", record
assert record["seated_minute"] is not None and record["seated_minute"] <= 30, record["seated_minute"]
assert (record["route"], record["recovered"]) == ("claude ai login", True), record
print(f"successor {record['successor']} held the front door seat at minute {record['seated_minute']}")
PY

cleanup
left="$(docker ps --all --quiet --filter "$owned")$(docker volume ls --quiet --filter "$owned")"
left+="$(docker network ls --quiet --filter "$owned")"
if [[ -n "$left" ]]; then
  printf 'the stack left containers, volumes or networks behind: %s\n' "$left" >&2
  exit 1
fi
printf 'stack removed with its volumes and network\n'
printf 'Master drill passed: the emptied front door seat got a master forced up on the claude ai login within thirty minutes\n'
