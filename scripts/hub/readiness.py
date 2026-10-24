from collections.abc import Callable

from scripts.events.bus import Bus
from scripts.events.config import BusConfig
from scripts.hub import lease
from scripts.hub.registry import Reactor
from scripts.swarm import readiness


class Readiness:
    def __init__(self, bus: Bus, source: readiness.Source | None = None, clock: lease.Clock = lease.wall_ms):
        self._bus = bus
        self._source = source
        self._clock = clock

    def reactor(self) -> Reactor:
        return Reactor("readiness", "swarm.*.readiness.requested", lambda _: None, singleton=True, poll=self.poll)

    def poll(self, guard: Callable[[], bool] = lambda: True) -> None:
        if not guard():
            return
        if self._source is None:
            if BusConfig.from_environ().backend == "memory":
                return
            self._source = readiness.Source()
        for slug in self._source.slugs():
            view = self._source.snapshot(slug)
            if not guard():
                return
            readiness.publish(self._bus, view, self._clock())
