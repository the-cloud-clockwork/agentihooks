import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from scripts.events.bus import Bus
from scripts.events.config import QueuePolicy
from scripts.events.envelope import Envelope, _count
from scripts.hub import lease

_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")

Handler = Callable[[Envelope], None]

REACTOR_POLICY = QueuePolicy(max_deliver=50)
# Nak delays, kept out of the policy because its backoff would also shorten the ack wait of a handler still running.
# With fifty attempts they span about four hours, an outage the bus default of five attempts would not survive.
RETRY_MS = (1_000, 5_000, 15_000, 60_000, 300_000)


@dataclass(frozen=True)
class Reactor:
    name: str
    subject: str
    handler: Handler
    singleton: bool = False
    poll: Callable[[Callable[[], bool]], None] | None = None
    policy: QueuePolicy = REACTOR_POLICY
    retry_ms: tuple[int, ...] = RETRY_MS

    def __post_init__(self):
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise ValueError(f"reactor name {self.name!r} must be lowercase letters, digits and dashes")
        if not isinstance(self.subject, str) or not self.subject:
            raise ValueError(f"reactor {self.name!r} needs a subject")
        if not callable(self.handler):
            raise ValueError(f"reactor {self.name!r} needs a callable handler")
        if not isinstance(self.retry_ms, tuple) or not all(map(_count, self.retry_ms)):
            raise ValueError(f"reactor {self.name!r} retry delays must be a tuple of non negative integers")


class Registry:
    def __init__(self, reactors: Iterable[Reactor] = ()):
        self._reactors: dict[str, Reactor] = {}
        for reactor in reactors:
            if reactor.name in self._reactors:
                raise ValueError(f"reactor {reactor.name!r} is declared twice")
            self._reactors[reactor.name] = reactor

    def __iter__(self) -> Iterator[Reactor]:
        return iter(self._reactors.values())


def durable(reactor: Reactor) -> str:
    return f"hub-{reactor.name}"


REACTORS = Registry()


def configured(bus: Bus, clock: lease.Clock = lease.wall_ms) -> Registry:
    from scripts.hub.hive_loss import HiveLoss
    from scripts.hub.lifecycle import Lifecycle
    from scripts.hub.pulls import Pulls
    from scripts.hub.readiness import Readiness
    from scripts.hub.reconciler import Reconciler
    from scripts.hub.timers import Timers

    return Registry(
        [
            *REACTORS,
            Timers(bus, clock).reactor(),
            HiveLoss(bus, clock).reactor(),
            Lifecycle(bus).reactor(),
            Reconciler(bus, clock=clock).reactor(),
            Readiness(bus, clock=clock).reactor(),
            Pulls(bus, clock=clock).reactor(),
        ]
    )
