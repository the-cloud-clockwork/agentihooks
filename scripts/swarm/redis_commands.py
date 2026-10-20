"""The Redis commands each swarm consumer reaches from its entry point, read from the code, and the scheduler keys it
writes, so a deployment can check that the Redis user serving each consumer grants every one of them."""

import argparse
import json
import os
import sys
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
    "ledger server": Consumer("ledger", ("scripts.swarm_ledger:run", "scripts.swarm_ledger.ledger_server:main")),
    "worker": Consumer("worker", ("scripts.swarm_v2.worker.start:main",)),
    "hooks": Consumer("worker", ("hooks.hook_manager:main",)),
}


def scheduler_keys(deployment: str) -> dict[str, str]:
    from scripts.scheduler.core import Scheduler

    with mock.patch.dict(os.environ, {"AGENTIHOOKS_DEPLOYMENT": deployment}):
        os.environ.pop("AGENTIHOOKS_HIVE_ID", None)
        scheduler = Scheduler(None, machine="probe")
        return {label: scheduler.key(suffix) for label, suffix in SCHEDULER_KEYS.items()}


def consumer_map(deployment: str, root: Path = ROOT) -> dict:
    """Each consumer's Redis role, the commands it reaches with the code that sends each, and its scheduler keys."""
    program = Program(parse(root))
    keys = scheduler_keys(deployment)
    consumers = {}
    for name, consumer in CONSUMERS.items():
        found = program.commands(program.reach(consumer.entries))
        commands = {command: sorted(units) for command, units in sorted(found.items())}
        consumers[name] = {"role": consumer.role, "commands": commands, "keys": keys}
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
