"""Hub reactors that end a checks or merge wait the moment a pull request event names its pull request.

The event only triggers the re-check: each matching wait goes through ``waits.end_one``, the same single wait end the
polled waits pass runs, so the resolution, the ended item, the woke key and the wait history are the polled pass's own.
"""

from collections.abc import Callable
from typing import Any

from scripts.events.envelope import Envelope
from scripts.hub import lease
from scripts.hub.registry import Reactor
from scripts.inbox.store import InboxStore
from scripts.swarm import idle, ledger_events, waits
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import AgentRecord, RedisStore, connect

SUBJECTS = {
    "wait-ends-merged": "swarm.*.pull.merged",
    "wait-ends-closed": "swarm.*.pull.closed",
    "wait-ends-dequeued": "swarm.*.merge.dequeued",
    "wait-ends-checks": "swarm.*.checks.completed",
}
KINDS = ("checks", "merge")


class WaitEnds:
    def __init__(
        self,
        store: RedisStore | None = None,
        ledger: Any = None,
        github: Callable[[str], PullRequest | None] = ledger_events.view,
        clock: lease.Clock = lease.wall_ms,
    ) -> None:
        self.store = store
        self.ledger = ledger or LedgerClient(service=True)
        self.github = github
        self.clock = clock

    def reactors(self) -> list[Reactor]:
        """One singleton reactor per pull request subject, all sharing this handler."""
        return [Reactor(name, subject, self.handle, singleton=True) for name, subject in SUBJECTS.items()]

    def handle(self, event: Envelope) -> None:
        """Re-check every checks or merge wait on the event's pull request and end those that resolved."""
        store = self.store = self.store or connect()
        held = _held(store, event.swarm, event.key)
        rows = {
            agent.task: row for agent, _ in held if agent.task and (row := self.ledger.task(event.swarm, agent.task))
        }
        reads = waits.Reads(rows, InboxStore(store.redis), self.github, self.github)
        for agent, wait in held:
            waits.end_one(store, event.swarm, agent, wait, reads, self.clock())


def _held(store: RedisStore, slug: str, url: str) -> list[tuple[AgentRecord, dict[str, Any]]]:
    """The live agents of ``slug`` holding a checks or merge wait on ``url``, with their waits."""
    found: list[tuple[AgentRecord, dict[str, Any]]] = []
    for agent in store.agents(slug):
        wait: dict[str, Any] = {} if agent.state == "finished" else idle.wait(store.redis, slug, agent.name) or {}
        on = wait.get("on") or {}
        if on.get("kind") in KINDS and on.get("target") == url:
            found.append((agent, wait))
    return found
