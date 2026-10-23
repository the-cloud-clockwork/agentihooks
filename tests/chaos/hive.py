import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from scripts.events.bus import StateStore
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.factory import open_bus
from scripts.hive import execution_events
from scripts.hive.spoke import Spoke, SpokeConfig
from scripts.swarm import readiness
from scripts.swarm_ledger.repository import hierarchy
from scripts.swarm_v2.runtime.base import Outcome, SpawnRequest, Status
from tests.chaos.journal import Journal, now_ms

LANE = "eng"
OFFERS = 10_000
GENERATIONS = "chaos-generations"
POLL_MS = 500
QUIET = frozenset({"empty", "held"})
# admission.room grants (80 * cpus - 100 * load5) // 50 launches, so these readings leave capacity minus running.
CPUS_PER_SLOT = 0.625
LOAD_PER_AGENT = 0.5


@dataclass
class Agent:
    task: str
    claim: str
    ends_ms: int


class ChaosRuntime:
    backend = "chaos"
    capabilities = frozenset()

    def __init__(self, journal: Journal, hive: str, launch_ms: int, work_ms: int):
        self.journal, self.hive, self.launch_ms, self.work_ms = journal, hive, launch_ms, work_ms
        self.agents: list[Agent] = []

    def spawn(self, request: SpawnRequest) -> Outcome:
        fence = request.task["claim_fence"]
        fields = {"task": request.task["id"], "claim": fence["key"], "revision": fence["revision"]}
        self.journal.write("launching", **fields)
        time.sleep(self.launch_ms / 1000)
        self.agents.append(Agent(request.task["id"], fence["key"], now_ms() + self.work_ms))
        self.journal.write("launched", **fields)
        placed = SimpleNamespace(pane_id=f"{self.hive}:{request.name}", harness="chaos", account="chaos")
        return Outcome("spawn", Status.OK, self.backend, value=placed)


class ChaosTasks:
    def __init__(self, journal: Journal):
        self.journal = journal

    def request(self, event: Envelope) -> SpawnRequest:
        task = {
            "id": event.key,
            "execution_id": f"{event.key}-{event.version}",
            "execution_generation": event.version,
            "claim_generation": event.version,
        }
        return SpawnRequest(config=None, lane=LANE, name=f"{LANE}-{event.key}", task=task)

    def reopen(self, event: Envelope) -> None:
        self.journal.write("reopened", task=event.key)


def finish(bus, runtime: ChaosRuntime, journal: Journal) -> None:
    now = now_ms()
    for agent in [agent for agent in runtime.agents if agent.ends_ms <= now]:
        entry = bus.state("spoke-work").get(agent.claim)
        state = json.loads(entry.value) if entry else {}
        if state.get("state") == "started":
            started = Envelope.from_dict(state["event"])
            exited = replace(started, id=f"exited:{started.id}", type="execution.exited", time=now, cause=started.id)
            execution_events.emit(bus, exited)
            journal.write("exited", task=agent.task, claim=agent.claim)
        else:
            journal.write("exit-unrecorded", task=agent.task, claim=agent.claim, state=state.get("state"))
        runtime.agents.remove(agent)


def work(bus, spoke: Spoke, runtime: ChaosRuntime, journal: Journal, hive: str) -> None:
    previous = None
    while True:
        finish(bus, runtime, journal)
        outcome = spoke.poll(POLL_MS)
        if outcome not in QUIET and (outcome != previous or outcome == "started"):
            journal.write("poll", outcome=outcome)
        previous = outcome
        if outcome == "retry":
            # Spoke.poll swallows a dead connection as retry, so a direct read raises it to reconnect.
            bus.state("spoke-pending").get(hive)
        if outcome not in QUIET | {"started"}:
            time.sleep(0.5)
        elif outcome == "held":
            time.sleep(0.2)


class OfferedRows(dict):
    def __init__(self, rows: dict, generations: Callable[[], StateStore]):
        super().__init__(rows)
        self.generations = generations

    def get(self, key, default=None):
        row = super().get(key, default)
        entry = self.generations().get(key) if row is not None else None
        return row if entry is None else {**row, "claim_generation": int(entry.value)}


def readiness_view(swarm: str, generations: Callable[[], StateStore]) -> Callable[[], readiness.Snapshot]:
    tasks = [
        {"id": f"t{n:04d}", "phase": "chaos", "state": "open", "lane": LANE, "claim_generation": 1}
        for n in range(1, OFFERS + 1)
    ]
    doc = {"tasks": tasks, "phases": [{"id": "chaos", "title": "chaos", "depends_on": []}]}
    rows = OfferedRows({task["id"]: task for task in tasks}, generations)
    view = readiness.Snapshot(swarm, rows, doc, hierarchy.project(doc)[0], "", lambda _: False, lambda _: True)
    return lambda: view


def record_generation(bus, task: str, generation: int) -> None:
    store = bus.state(GENERATIONS)
    entry = store.get(task)
    if entry is None:
        store.create(task, str(generation).encode())
    else:
        store.update(task, str(generation).encode(), entry.revision)


def serve(args: argparse.Namespace) -> None:
    journal = Journal(Path(args.journal), args.hive)
    runtime = ChaosRuntime(journal, args.hive, args.launch_ms, args.work_ms)
    tasks = ChaosTasks(journal)
    config = SpokeConfig(swarm=SimpleNamespace(slug=args.swarm), lane=LANE, hive=args.hive)
    current = {}
    view = readiness_view(args.swarm, lambda: current["bus"].state(GENERATIONS))

    def measure() -> dict:
        return {"cpus": args.capacity * CPUS_PER_SLOT, "load5": len(runtime.agents) * LOAD_PER_AGENT}

    while True:
        try:
            bus = open_bus(BusConfig(backend="nats", url=args.url))
        except Exception:
            time.sleep(0.5)
            continue
        current["bus"] = bus
        journal.write("connected")
        try:
            spoke = Spoke(config, bus, runtime, tasks, measure=measure, view=view)
            work(bus, spoke, runtime, journal, args.hive)
        except Exception as exc:
            journal.write("disconnected", error=type(exc).__name__)
        try:
            bus.close()
        except Exception:
            pass


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="chaos hive")
    for flag in ("--url", "--swarm", "--hive", "--journal"):
        parser.add_argument(flag, required=True)
    parser.add_argument("--capacity", type=int, default=4)
    parser.add_argument("--launch-ms", type=int, default=1500)
    parser.add_argument("--work-ms", type=int, default=6000)
    serve(parser.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
