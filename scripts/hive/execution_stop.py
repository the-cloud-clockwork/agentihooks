"""Execution stop on the events engine: the hub asks the hive spoke that started an execution to reap it, and that
spoke ends it through the normal execution exited event."""

from typing import Any

from scripts.events.bus import Bus
from scripts.events.envelope import Envelope
from scripts.swarm.store import AgentRecord

SENT = "sent"
UNKNOWN = "unknown"
ENDED = "ended"
FOREIGN = "foreign"
STOPPED = "stopped"
FAILED = "failed"
REASON = "stopped"


def request(started: Envelope, reason: str) -> Envelope:
    """The stop event for one started execution."""
    raise NotImplementedError


def stop(bus: Bus, slug: str, agent: AgentRecord, reason: str) -> str:
    """Publishes the stop for one admitted execution to the hive that started it."""
    raise NotImplementedError


class Stopper:
    """A hive's reader of the stops addressed to it in one swarm."""

    def __init__(self, bus: Bus, swarm: str, hive: str, runtime: Any) -> None:
        self.bus, self.swarm, self.hive, self.runtime = bus, swarm, hive, runtime

    def poll(self, timeout_ms: int = 0) -> list[str]:
        """Settles every stop waiting for this hive and answers each outcome."""
        raise NotImplementedError

    def handle(self, event: Envelope) -> str:
        """Reaps the execution one stop names when this hive owns it and it has not ended."""
        raise NotImplementedError
