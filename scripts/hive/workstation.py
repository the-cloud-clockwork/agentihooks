import itertools
import os
from collections.abc import Callable, Iterable, Mapping

from scripts.events.envelope import Envelope
from scripts.hive.spoke import Spoke, SpokeConfig
from scripts.scheduler.process_timer import wait as scheduled_wait
from scripts.swarm_v2.runtime.base import SpawnRequest

DEPLOYMENT_ENV = "AGENTIHOOKS_DEPLOYMENT"
DISTRIBUTED = "distributed"
LANE = "master"
IDENTITY = ("execution_id", "execution_generation", "claim_generation")
NO_ENGINE = "the hub runs the swarm engine in distributed mode; this workstation only takes master work as a spoke"
ENGINE_RUNS = "the workstation spoke runs only in distributed mode; this workstation runs the swarm engine"
TAKE_MS = 1_000
IDLE_S = 5.0


def runs_engine(environ: Mapping[str, str] = os.environ) -> bool:
    return environ.get(DEPLOYMENT_ENV) != DISTRIBUTED


class MasterTasks:
    def __init__(self, configs: Callable[[str], object]):
        self.configs = configs

    def request(self, event: Envelope) -> SpawnRequest | None:
        data = event.data
        if data.get("lane") != LANE or not all(field in data for field in ("name", *IDENTITY)):
            return None
        task = {"id": event.key, **{field: data[field] for field in IDENTITY}}
        return SpawnRequest(self.configs(event.swarm), LANE, data["name"], task)

    def reopen(self, event: Envelope) -> None:
        return None


def spokes(store, bus, runtime, hive: str, held: dict[str, Spoke]) -> list[Spoke]:
    slugs = store.slugs()
    for slug in slugs:
        if slug not in held:
            held[slug] = Spoke(SpokeConfig(store.config(slug), LANE, hive), bus, runtime, MasterTasks(store.config))
    return [held[slug] for slug in slugs]


def run_once(store, bus, runtime, hive: str, timeout_ms: int, held: dict[str, Spoke]) -> list[str]:
    return [spoke.poll(timeout_ms) for spoke in spokes(store, bus, runtime, hive, held)]


def run(
    store,
    bus,
    runtime,
    hive: str,
    wait: Callable[[float], object] = scheduled_wait,
    passes: Iterable[int] | None = None,
) -> None:
    held: dict[str, Spoke] = {}
    for _ in itertools.count() if passes is None else passes:
        if "started" not in run_once(store, bus, runtime, hive, TAKE_MS, held):
            wait(IDLE_S)
