from collections.abc import Callable
from dataclasses import replace

from scripts.swarm.store import RedisStore
from scripts.swarm_v2.herdr.publications import TransportRefused
from scripts.swarm_v2.herdr.reconcile import Report
from scripts.swarm_v2.herdr.upkeep import Attempt, Upkeep
from scripts.swarm_v2.kubernetes import jump
from scripts.swarm_v2.kubernetes.terminal import HostKeys
from scripts.swarm_v2.operator_auth import OPERATOR

TRANSPORT = "transport"


def attempts(store: RedisStore) -> list[Attempt]:
    keys = HostKeys(store)
    return [
        attempt
        for slug in store.slugs()
        for attempt in keys.attempts(slug, store.execution_occupants(slug).values(), OPERATOR)
    ]


class Service:
    def __init__(self, upkeep: Upkeep, connect: Callable[[], RedisStore]) -> None:
        self.upkeep, self.connect = upkeep, connect

    def run_pass(self) -> Report | None:
        try:
            store = self.connect()
        except TransportRefused as refused:
            return Report(refused={TRANSPORT: str(refused)})
        terminal = self.upkeep.terminal
        found = jump.recorded(store)
        if found is not None and found != terminal.home.jump:
            terminal.home = replace(terminal.home, jump=found)
            self.upkeep.due = True
        return self.upkeep.change(attempts(store))
