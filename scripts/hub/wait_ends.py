"""Hub reactors that end a checks or merge wait the moment a pull request event names its pull request.

The event only triggers the re-check: each matching wait goes through ``waits.end_one``, the same single wait end the
polled waits pass runs, so the resolution, the ended item, the woke key and the wait history are the polled pass's own.
"""

import sys
from collections.abc import Callable
from importlib import import_module
from typing import Any

from scripts.events.envelope import Envelope
from scripts.hub import lease
from scripts.hub.registry import Reactor
from scripts.inbox.store import CLOSED, Gone, InboxStore
from scripts.swarm import ledger_events, waits
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import MASTER, AgentRecord, RedisStore

idle: Any = import_module("scripts.swarm.idle")
store_module: Any = import_module("scripts.swarm.store")

SUBJECTS = {
    "wait-ends-merged": "swarm.*.pull.merged",
    "wait-ends-closed": "swarm.*.pull.closed",
    "wait-ends-dequeued": "swarm.*.merge.dequeued",
    "wait-ends-checks": "swarm.*.checks.completed",
}
KINDS = ("checks", "merge")
TASK_SUBJECT = "swarm.*.task.*"
REPLY_SUBJECT = "swarm.*.inbox-wake.requested"
DEPENDENCY_CLOSED = "Task {closed}, which your task {task} depends on, has closed as done."


class AnswerPending(RuntimeError):
    def __init__(self, item_id: str) -> None:
        super().__init__(f"answered item {item_id} is still open")


class WaitEnds:
    def __init__(
        self,
        store: RedisStore | None = None,
        ledger: Any = None,
        github: Callable[[str], PullRequest | None] = ledger_events.view,
        clock: lease.Clock = lease.wall_ms,
    ) -> None:
        self.store: Any = store
        self.ledger: Any = ledger or LedgerClient(service=True)
        self.github = github
        self.clock = clock

    def reactors(self) -> list[Reactor]:
        """One singleton reactor per pull request subject sharing the pull handler, then the task and reply ends."""
        return [
            *(Reactor(name, subject, self.handle, singleton=True) for name, subject in SUBJECTS.items()),
            Reactor("task-wait-ends", TASK_SUBJECT, self.task_changed, singleton=True),
            Reactor("reply-wait-ends", REPLY_SUBJECT, self.replied, singleton=True),
        ]

    def handle(self, event: Envelope) -> None:
        """Re-check every checks or merge wait on the event's pull request and end those that resolved."""
        store = self.store = self.store or store_module.connect()
        held = _held(store, event.swarm, event.key)
        rows = {
            agent.task: row for agent, _ in held if agent.task and (row := self.ledger.task(event.swarm, agent.task))
        }
        _end_each(store, event.swarm, held, self._reads(rows, InboxStore(store.redis)), self.clock())

    def task_changed(self, event: Envelope) -> None:
        """End every task wait on a task the event turned done or blocked, then tell the live holders of its
        dependents once it is done."""
        envelope: Any = event
        if event.type != "task.changed" or envelope.data.get("state") not in waits.TASK_ENDS:
            return
        store = self.store = self.store or store_module.connect()
        row: dict[str, Any] | None = self.ledger.task(event.swarm, event.key)
        reads = self._reads({event.key: row} if row else {}, InboxStore(store.redis))
        _end_each(store, event.swarm, _held(store, event.swarm, event.key, ("task",)), reads, self.clock())
        if row and row.get("state") == "done":
            self._tell_dependents(store, event.swarm, event.key)

    def replied(self, event: Envelope) -> None:
        """End every reply wait on the item the woken message answers, failing while that item is still open so the
        hub redelivers the wake after it closes."""
        store = self.store = self.store or store_module.connect()
        inbox = InboxStore(store.redis)
        try:
            answered = inbox.locate(event.key).ref
        except Gone:
            return
        if not answered:
            return
        held = _held(store, event.swarm, answered, ("reply",))
        if held and _still_open(inbox, answered):
            raise AnswerPending(answered)
        _end_each(store, event.swarm, held, self._reads({}, inbox), self.clock())

    def _reads(self, rows: dict[str, dict[str, Any]], inbox: InboxStore) -> waits.Reads:
        """What ending a wait reads: ``rows`` and ``inbox`` with this reactor's pull request reader."""
        return waits.Reads(rows, inbox, self.github, self.github)

    def _tell_dependents(self, store: Any, slug: str, closed: str) -> None:
        """Send the live holder of every claimed open task whose depends_on names ``closed`` one item naming it."""
        mail: Any = ledger_events.Mail(InboxStore(store.redis), store, slug)
        agents: list[AgentRecord] = store.agents(slug)
        for agent in agents:
            if agent.state == "finished" or agent.lane == MASTER or agent.task in ("", closed):
                continue
            row: dict[str, Any] = self.ledger.task(slug, agent.task) or {}
            if (
                row.get("claimed_by") == agent.name
                and row.get("state") not in waits.TASK_ENDS
                and closed in (row.get("depends_on") or [])
            ):
                text = DEPENDENCY_CLOSED.format(closed=closed, task=agent.task)
                mail.send(f"dependency-closed:{closed}:{agent.task}", agent.seat or agent.name, text)


def _end_each(
    store: Any, slug: str, held: list[tuple[AgentRecord, dict[str, Any]]], reads: waits.Reads, now_ms: int
) -> None:
    """End each held wait through ``waits.end_one``, so one failing wait leaves the others ending."""
    for agent, wait in held:
        try:
            waits.end_one(store, slug, agent, wait, reads, now_ms)
        except Exception as exc:
            print(f"wait of {agent.name} failed: {exc}", file=sys.stderr)


def _still_open(inbox: InboxStore, item_id: str) -> bool:
    """Whether the inbox item ``item_id`` still exists and has not closed."""
    try:
        return inbox.locate(item_id).state not in CLOSED
    except Gone:
        return False


def _held(
    store: Any, slug: str, target: str, kinds: tuple[str, ...] = KINDS
) -> list[tuple[AgentRecord, dict[str, Any]]]:
    """The live agents of ``slug`` holding a wait of one of ``kinds`` on ``target``, with their waits."""
    found: list[tuple[AgentRecord, dict[str, Any]]] = []
    agents: list[AgentRecord] = store.agents(slug)
    for agent in agents:
        wait: dict[str, Any] = {} if agent.state == "finished" else idle.wait(store.redis, slug, agent.name) or {}
        on: dict[str, Any] = wait.get("on") or {}
        if on.get("kind") in kinds and on.get("target") == target:
            found.append((agent, wait))
    return found
