"""Hub reactors that end a checks or merge wait the moment a pull request event names its pull request."""

from collections.abc import Callable
from typing import Any

from scripts.events.envelope import Envelope
from scripts.hub import lease
from scripts.hub.registry import Reactor
from scripts.swarm import ledger_events
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import RedisStore


class WaitEnds:
    def __init__(
        self,
        store: RedisStore | None = None,
        ledger: Any = None,
        github: Callable[[str], PullRequest | None] = ledger_events.view,
        clock: lease.Clock = lease.wall_ms,
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.github = github
        self.clock = clock

    def reactors(self) -> list[Reactor]:
        """One singleton reactor per pull request subject, all sharing this handler."""
        return []

    def handle(self, event: Envelope) -> None:
        """Re-check every checks or merge wait on the event's pull request and end those that resolved."""
