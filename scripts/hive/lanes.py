"""Eng, ci and plan lane spokes a hive runs for every swarm on the events engine; the hub admits each offer taken."""

import itertools
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace

from scripts.events.config import BACKEND_ENV, DEFAULT_BACKEND, URL_ENV
from scripts.events.envelope import Envelope
from scripts.hive import account_slots, workstation
from scripts.hive.spoke import Spoke, SpokeConfig
from scripts.scheduler.process_timer import wait as scheduled_wait
from scripts.swarm import readiness
from scripts.swarm_v2.runtime.base import SpawnRequest

LANES = ("eng", "ci", "plan")
EXCLUDED_ENV = "HIVE_EXCLUDED"
TAKE_MS = workstation.TAKE_MS
IDLE_S = workstation.IDLE_S
RETRY_S = 30.0


class LaneTasks:
    def __init__(self, bus, hive: str, configs: Callable[[str], object]):
        self.bus, self.hive, self.configs = bus, hive, configs

    def _ask(self, op: str, event: Envelope):
        ask = account_slots.asker(self.bus.for_swarm(event.swarm), event.swarm, self.hive)
        request = {
            "op": op,
            "hive": self.hive,
            "task": event.key,
            "lane": event.data["lane"],
            "generation": event.version,
        }
        return ask(request)

    def request(self, event: Envelope) -> SpawnRequest | None:
        admitted = self._ask("admit", event)
        if admitted is None:
            return None
        config = self.configs(event.swarm)
        if config.repo_origin:
            config = replace(config, repo=config.repo_origin)
        return SpawnRequest(config, event.data["lane"], admitted["name"], admitted["task"])

    def reopen(self, event: Envelope) -> None:
        self._ask("reopen", event)


def lanes(excluded: str | None) -> tuple[str, ...]:
    left_out = excluded.split(",") if excluded else ()
    return tuple(lane for lane in LANES if lane not in left_out)


def spokes(store, bus, runtime, hive: str, taken: Iterable[str], held: dict) -> list[Spoke]:
    current = []
    for slug in readiness.Source(store).slugs():
        config = store.config(slug)
        for lane in taken:
            if (slug, lane) not in held:
                tasks = LaneTasks(bus, hive, store.config)
                held[slug, lane] = Spoke(SpokeConfig(config, lane, hive), bus, runtime, tasks, view=lambda: None)
                queue = readiness.queue_name(slug, lane)
                print(f"hive spoke for swarm {slug} lane {lane} takes {queue}", file=sys.stderr)
            current.append(held[slug, lane])
    return current


def run_once(store, bus, runtime, hive: str, taken: Iterable[str], timeout_ms: int, held: dict) -> list[str]:
    return [spoke.poll(timeout_ms) for spoke in spokes(store, bus, runtime, hive, taken, held)]


def run(
    store,
    bus,
    runtime,
    hive: str,
    taken: Iterable[str],
    wait: Callable[[float], object] = scheduled_wait,
    passes: Iterable[int] | None = None,
) -> None:
    held: dict = {}
    for _ in itertools.count() if passes is None else passes:
        if "started" not in run_once(store, bus, runtime, hive, taken, TAKE_MS, held):
            wait(IDLE_S)


def _loop(environ: Mapping[str, str]) -> None:
    from scripts.events import factory
    from scripts.events.config import BusConfig
    from scripts.swarm import commands
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.store import connect
    from scripts.swarm_v2.runtime.local import LocalHerdrRuntime

    bus = factory.open_bus(replace(BusConfig.from_environ(environ), provision=False))
    try:
        runtime = LocalHerdrRuntime(HerdrRuntime())
        run(connect(), bus, runtime, commands.hive_id(), lanes(environ.get(EXCLUDED_ENV)))
    finally:
        bus.close()


def _serve(
    environ: Mapping[str, str],
    wait: Callable[[float], object] = scheduled_wait,
    sleep: Callable[[float], object] = time.sleep,
) -> None:
    while True:
        try:
            _loop(environ)
        except Exception:
            _stopped(sys.exception())
            _retry_wait(wait, sleep)


def _retry_wait(wait: Callable[[float], object], sleep: Callable[[float], object]) -> None:
    try:
        wait(RETRY_S)
    except Exception:
        _wait_failed(sys.exception())
        sleep(RETRY_S)


def _stopped(exc: BaseException | None) -> None:
    print(f"hive lane spokes stopped ({type(exc).__name__}: {exc}); starting again", file=sys.stderr)


def _wait_failed(exc: BaseException | None) -> None:
    print(f"hive lane spokes retry wait failed ({type(exc).__name__}: {exc}); sleeping instead", file=sys.stderr)


def serve(environ: Mapping[str, str] = os.environ) -> threading.Thread | None:
    if workstation.runs_engine(environ):
        return None
    if (environ.get(BACKEND_ENV) or DEFAULT_BACKEND) == "memory":
        print(
            "hive lane spokes refused: the memory bus never sees the hub's ready offers; "
            f"a distributed hive needs {BACKEND_ENV}=nats and {URL_ENV}",
            file=sys.stderr,
        )
        return None
    thread = threading.Thread(target=_serve, args=(environ,), name="lane-spokes", daemon=True)
    thread.start()
    return thread
