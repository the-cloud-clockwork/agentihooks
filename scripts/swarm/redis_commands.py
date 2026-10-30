"""The Redis commands each swarm consumer reaches from its entry point, read from the code, and the scheduler keys it
writes, so a deployment can check that the Redis user serving each consumer grants every one of them."""

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from scripts.swarm.code_reach import Program, parse

ROOT = Path(__file__).resolve().parents[2]
SCHEDULER_KEYS = {"scheduler entries": "entries", "scheduler wake": "wake:probe"}


@dataclass(frozen=True)
class Consumer:
    role: str
    entries: tuple[str, ...]


CONSUMERS = {
    "controller": Consumer("controller", ("scripts.swarm.controller:main",)),
    "tick": Consumer("controller", ("scripts.swarm.scheduled_tick:main", "scripts.swarm.cli:cmd_tick")),
    "hub": Consumer("controller", ("scripts.hub.runner:main",)),
    "ledger server": Consumer("ledger", ("scripts.swarm_ledger:run", "scripts.swarm_ledger.ledger_server:main")),
    "worker": Consumer("worker", ("scripts.swarm_v2.worker.start:main",)),
    "hooks": Consumer("worker", ("hooks.hook_manager:main",)),
}
STATE_STORE = "scripts.events.redis_state:RedisStateStore."
PROTOCOL = "scripts.events.bus:StateStore."
KEYLESS = frozenset({"MULTI", "EXEC"})


@dataclass(frozen=True)
class Store:
    opener: str
    open: Callable
    # Reached only through scope.members, which a hive member's hooks skip: they read their own scope alone.
    skipped: tuple[str, ...] = ()


def _credentials():
    from scripts.hive import auth

    return auth._store(None)


def _registry():
    from scripts.hive import hive_state

    return hive_state.records(None, "probe")


STORES = {
    "hive credentials": Store("scripts.hive.auth:_store", _credentials, ("hooks",)),
    "hive registry": Store("scripts.hive.hive_state:records", _registry),
}


def scheduler_keys(deployment: str) -> dict[str, str]:
    from scripts.scheduler.core import Scheduler

    with mock.patch.dict(os.environ, {"AGENTIHOOKS_DEPLOYMENT": deployment}):
        os.environ.pop("AGENTIHOOKS_HIVE_ID", None)
        scheduler = Scheduler(None, machine="probe")
        return {label: scheduler.key(suffix) for label, suffix in SCHEDULER_KEYS.items()}


def store_keys(store: Store) -> list[str]:
    from scripts.hive import hive_state

    with mock.patch.object(hive_state, "on_redis", lambda: True):
        opened = store.open()
    return [opened._current, opened._revision, f"{opened._history}probe", opened._projected, opened._published]


def consumer_map(deployment: str, root: Path = ROOT) -> dict:
    """Each consumer's Redis role, the commands it reaches with the code that sends each, its scheduler keys, and the
    keys of each hive state store it opens with every command the storage protocol may send on them."""
    program = Program(parse(root))
    keys = scheduler_keys(deployment)
    protocol = tuple(STATE_STORE + unit.removeprefix(PROTOCOL) for unit in program.units if unit.startswith(PROTOCOL))
    sent = program.commands(program.reach(protocol))
    store_commands = {command: sorted(units) for command, units in sorted(sent.items()) if command not in KEYLESS}
    consumers = {}
    for name, consumer in CONSUMERS.items():
        reached = program.reach(consumer.entries)
        found = program.commands(reached)
        commands = {command: sorted(units) for command, units in sorted(found.items())}
        stores = {
            label: {"keys": store_keys(store), "commands": store_commands}
            for label, store in STORES.items()
            if store.opener in reached and name not in store.skipped
        }
        consumers[name] = {"role": consumer.role, "commands": commands, "keys": keys, "stores": stores}
    return {"deployment": deployment, "consumers": consumers}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", default="distributed")
    args = parser.parse_args(argv)
    json.dump(consumer_map(args.deployment), sys.stdout, indent=1, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
