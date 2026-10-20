import json
import os
import subprocess
import sys
import time

from scripts.hive import registry
from scripts.hive.orders import Consumer
from scripts.swarm import lease
from scripts.swarm.capacity import Account
from scripts.swarm.controller import FencedRuntime
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord, SwarmConfig, connect
from scripts.swarm.tick import Placed
from scripts.swarm_v2.runtime.base import RuntimeRouter
from scripts.swarm_v2.runtime.local import LocalHerdrRuntime
from scripts.swarm_v2.runtime.routed import RoutedRuntime

SLUG = "compose-launch-proof"
HIVE = "remote-worker"
READY = "hive-launch-proof:ready"
STARTED = "hive-launch-proof:started"
DONE = "hive-launch-proof:done"


class ScratchHerdr:
    def __init__(self) -> None:
        self.children = []

    def spawn(self, config: SwarmConfig, lane: str, name: str, task: dict) -> Placed:
        child = subprocess.Popen([sys.executable, __file__, "agent"])
        self.children.append(child)
        return Placed(
            "scratch-pane",
            "claude",
            "scratch-account",
            profile=task["launch_assignment"]["profile"],
            model="scratch",
            effort="medium",
            profile_decision={"validation": {"pid": child.pid}},
        )


def worker() -> None:
    store = connect()
    hive = registry._default(HIVE)
    hive.update(
        roles=["eng", "ci"],
        max_agents=2,
        slots=[{"harness": "claude", "account": "scratch-account", "kind": "subscription"}],
        repos=["scratch-repo"],
    )
    scratch = ScratchHerdr()
    consumer = Consumer(store.redis, HIVE, LocalHerdrRuntime(scratch))
    try:
        while not store.redis.exists(DONE):
            hive["heartbeat_at"] = lease.now_ms(store)
            with store.redis.pipeline() as pipe:
                pipe.hset(
                    registry.key(HIVE), mapping={key: registry._encode(key, value) for key, value in hive.items()}
                )
                pipe.sadd(registry.INDEX, HIVE)
                pipe.set(READY, "ready", ex=10)
                pipe.execute()
            consumer.poll()
            time.sleep(0.1)
    finally:
        for child in scratch.children:
            child.terminate()
            child.wait(timeout=5)


def agent() -> None:
    store = connect()
    store.redis.set(STARTED, json.dumps({"host": HIVE, "pid": os.getpid()}))
    time.sleep(300)


def controller() -> None:
    store = connect()
    config = SwarmConfig(SLUG, "scratch-repo", 1, 1, lanes={"eng": {"agent": "claude", "profile": "engineer"}})
    store.create(config)
    held = lease.acquire(store, SLUG, "compose-controller")
    herdr = HerdrRuntime()
    herdr._quota_accounts = [Account("claude", "scratch-account", "room", 0, 100, 100, 2)]
    runtime = FencedRuntime(
        store, SLUG, held, RoutedRuntime(herdr, RuntimeRouter([LocalHerdrRuntime(herdr)])), "compose"
    )
    name = store.next_name(SLUG, "eng")
    record = AgentRecord(name, "eng", "task", seat=f"eng-1@{SLUG}", state="starting")
    store.put_agent(SLUG, record)
    store.seats.occupy(record.seat, name, lease.now_ms(store))
    task = {"id": "task", "title": "Hive launch proof", "profile": "engineer", "seat": record.seat}
    with lease.kept(store, SLUG, held):
        placed = runtime.spawn(config, "eng", name, task)
        assert placed.execution.runtime_target["host_id"] == HIVE
        for _ in range(100):
            raw = store.redis.get(STARTED)
            if raw:
                break
            time.sleep(0.1)
        started = json.loads(raw)
        assert started == {"host": HIVE, "pid": placed.execution.runtime_target["pid"]}
        assert placed.execution.generation == 1
        print(
            json.dumps(
                {
                    "result": "remote scratch agent launched",
                    "hive": HIVE,
                    "execution": placed.execution.execution_id,
                    "generation": placed.execution.generation,
                    "pid": started["pid"],
                }
            ),
            flush=True,
        )
    store.redis.set(DONE, "done")


def main() -> None:
    mode = sys.argv[1]
    if mode == "ready":
        assert connect().redis.exists(READY)
    else:
        {"worker": worker, "agent": agent, "controller": controller}[mode]()


if __name__ == "__main__":
    main()
