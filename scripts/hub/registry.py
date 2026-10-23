import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from scripts.events.bus import Bus
from scripts.events.envelope import Envelope
from scripts.hub import lease

_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")

Handler = Callable[[Envelope], None]


@dataclass(frozen=True)
class Reactor:
    name: str
    subject: str
    handler: Handler
    singleton: bool = False
    poll: Callable[[Callable[[], bool]], None] | None = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise ValueError(f"reactor name {self.name!r} must be lowercase letters, digits and dashes")
        if not isinstance(self.subject, str) or not self.subject:
            raise ValueError(f"reactor {self.name!r} needs a subject")
        if not callable(self.handler):
            raise ValueError(f"reactor {self.name!r} needs a callable handler")


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
    from scripts.hub.lifecycle import Lifecycle
    from scripts.hub.timers import Timers

    return Registry([*REACTORS, Timers(bus, clock).reactor(), Lifecycle(bus).reactor()])
