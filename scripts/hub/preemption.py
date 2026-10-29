"""Operator priority preemption for swarms on the events engine, which the legacy tick skips."""

import sys
from collections.abc import Callable

from scripts.events.config import BusConfig
from scripts.hub import lease
from scripts.hub.registry import Reactor
from scripts.swarm import preempt, readiness
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import RedisStore, connect

# The legacy tick cadence: a waiting task is asked for only on the pass after the one that first saw it waiting.
INTERVAL_MS = 60_000


class Preemption:
    def __init__(
        self,
        store: RedisStore | None = None,
        ledger: LedgerClient | None = None,
        clock: lease.Clock = lease.wall_ms,
    ):
        self._store, self._ledger, self._clock = store, ledger, clock
        self._source: readiness.Source | None = None
        self._due = 0

    def reactor(self) -> Reactor:
        return Reactor("preemption", "swarm.*.preemption.requested", lambda _: None, singleton=True, poll=self.poll)

    def poll(self, guard: Callable[[], bool] = lambda: True) -> None:
        now = self._clock()
        if now < self._due or not guard():
            return
        if self._source is None:
            if self._store is None and BusConfig.from_environ().backend == "memory":
                return
            self._store = self._store or connect()
            self._source = readiness.Source(self._store, self._ledger or LedgerClient(service=True))
        for slug in self._source.slugs():
            if not guard():
                return
            self._preempt(slug, now)
        self._due = now + INTERVAL_MS

    def _preempt(self, slug: str, now: int) -> None:
        from scripts.swarm import tick

        config = self._store.config(slug)
        if config.state != "running":
            return
        doc = self._source.state(slug)
        rows = {task["id"]: task for task in doc["tasks"]}
        # The hub has no runtime to probe; hives record placement refusals in placement_waits, which the step reads.
        seats = preempt.Seats(rows, tick._ended, tick._current_owner, tick.ready_in_order, preempt._places)
        for action in tick.skip_refused(preempt.run, slug, config, self._store, doc, now, seats):
            print(f"hub: preemption swarm={slug}: {action}", file=sys.stderr)
