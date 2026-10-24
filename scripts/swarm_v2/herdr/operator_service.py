import time
from collections.abc import Callable
from dataclasses import asdict, replace

from scripts.swarm.store import RedisStore
from scripts.swarm_v2.herdr.attach import Jump
from scripts.swarm_v2.herdr.endpoints import CERTIFICATE_SECONDS
from scripts.swarm_v2.herdr.publications import TransportRefused
from scripts.swarm_v2.herdr.reconcile import Report
from scripts.swarm_v2.herdr.upkeep import Attempt, Upkeep
from scripts.swarm_v2.kubernetes import jump
from scripts.swarm_v2.kubernetes.terminal import HostKeys
from scripts.swarm_v2.operator_auth import OPERATOR

TRANSPORT = "transport"
RENEW_SECONDS = CERTIFICATE_SECONDS // 2


def attempts(store: RedisStore) -> list[Attempt]:
    keys = HostKeys(store)
    return [
        attempt
        for slug in store.slugs()
        for attempt in keys.attempts(slug, store.execution_occupants(slug).values(), OPERATOR)
    ]


class Service:
    def __init__(
        self, upkeep: Upkeep, connect: Callable[[], RedisStore], clock: Callable[[], float] = time.time
    ) -> None:
        self.upkeep, self.connect, self.clock = upkeep, connect, clock
        self.synced: float | None = None

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
        now = self.clock()
        if self.synced is not None and now - self.synced >= RENEW_SECONDS:
            self.upkeep.due = True
        report = self.upkeep.change(attempts(store))
        if report is not None:
            self.synced = now
        return report

    def saved(self) -> dict:
        jump = self.upkeep.terminal.home.jump
        return {
            "due": self.upkeep.due,
            "seen": self.upkeep.seen,
            "synced": self.synced,
            "jump": None if jump is None else asdict(jump),
        }

    def resume(self, saved: dict) -> None:
        self.upkeep.due, self.upkeep.seen, self.synced = saved["due"], saved["seen"], saved["synced"]
        if saved["jump"] is not None:
            terminal = self.upkeep.terminal
            terminal.home = replace(terminal.home, jump=Jump(**saved["jump"]))
