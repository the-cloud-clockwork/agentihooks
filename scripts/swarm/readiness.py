"""Which tasks a lane may claim, offered to the lane's work queue on every hub pass.

An offer's id carries the task's claim generation, so the bus drops a repeat only inside its dedupe window.
"""

from collections.abc import Callable
from dataclasses import dataclass

from scripts.events import subjects
from scripts.events.bus import Bus, Delivery, WorkQueue
from scripts.events.config import QueuePolicy
from scripts.events.envelope import Envelope
from scripts.swarm import apply_wait, claim_hold, freeze, release_wait
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import EVENTS_ENGINE, RedisStore, SwarmError, connect
from scripts.swarm_ledger.repository import hierarchy

AUTHOR = "readiness"


@dataclass(frozen=True)
class Snapshot:
    slug: str
    rows: dict
    doc: dict
    graph: dict
    fix: str
    claimed: Callable[[str], bool]
    admits: Callable[[dict], bool]


def _on_events(store: RedisStore, slug: str) -> bool:
    try:
        return store.config(slug).engine == EVENTS_ENGINE
    except SwarmError:
        return False


class Source:
    def __init__(self, store: RedisStore | None = None, ledger: LedgerClient | None = None):
        self._store = store
        self._ledger = ledger or LedgerClient(service=True)

    def slugs(self) -> list[str]:
        self._store = self._store or connect()
        return [slug for slug in self._store.slugs() if _on_events(self._store, slug)]

    def snapshot(self, slug: str) -> Snapshot:
        self._store = self._store or connect()
        config = self._store.config(slug)
        doc = self._ledger.state(slug)
        rows = {task["id"]: task for task in doc["tasks"]}
        owned = {agent.task for agent in self._store.agents(slug) if agent.state != "finished"}
        released = release_wait.checker(config.repo)
        applied = apply_wait.checker(config, rows)
        return Snapshot(
            slug,
            rows,
            doc,
            hierarchy.project(doc)[0],
            freeze.fix_phase(config),
            lambda task_id: task_id in owned or self._store.claimant(slug, task_id) is not None,
            lambda task: config.state == "running" and released(task) and applied(task),
        )


def ready(task: dict, rows: dict, doc: dict, graph: dict, fix: str) -> bool:
    return (
        task.get("state") == "open"
        and not task.get("out_of_scope")
        and not task.get("merged_into")
        and not claim_hold.hold(task, rows, doc, graph, fix)
    )


def claimable(task: dict, lane: str, view: Snapshot) -> bool:
    return (
        task.get("lane") == lane
        and ready(task, view.rows, view.doc, view.graph, view.fix)
        and not view.claimed(task["id"])
        and view.admits(task)
    )


def generation(task: dict) -> int:
    return task.get("claim_generation") or 0


def offer_id(slug: str, task: dict) -> str:
    return f"ready:{slug}:{task['id']}:{generation(task)}"


def queue_name(slug: str, lane: str) -> str:
    return subjects.subject(slug, "ready", lane)


def offer(task: dict, view: Snapshot, now_ms: int) -> Envelope:
    return Envelope(
        id=offer_id(view.slug, task),
        type="task.ready",
        swarm=view.slug,
        entity="task",
        key=task["id"],
        version=generation(task),
        time=now_ms,
        author=AUTHOR,
        data={"lane": task["lane"], "generation": generation(task)},
    )


def offers(view: Snapshot, now_ms: int) -> list[Envelope]:
    return [
        offer(task, view, now_ms)
        for task in view.rows.values()
        if task.get("lane") and claimable(task, task["lane"], view)
    ]


def publish(bus: Bus, view: Snapshot, now_ms: int, policy: QueuePolicy = QueuePolicy()) -> list[str]:
    placed = []
    for event in offers(view, now_ms):
        if bus.queue(queue_name(view.slug, event.data["lane"]), policy).offer(event):
            placed.append(event.id)
    return placed


def withdrawal(event: Envelope, view: Snapshot) -> str:
    task = view.rows.get(event.key)
    if task is None:
        return "the task is gone"
    if view.claimed(task["id"]):
        return "it is claimed"
    if task.get("state") != "open":
        return f"it is {task.get('state')}"
    if generation(task) != event.version:
        return f"it is at generation {generation(task)}"
    if task.get("lane") != event.data["lane"]:
        return f"it moved to lane {task.get('lane')}"
    if not ready(task, view.rows, view.doc, view.graph, view.fix):
        return claim_hold.hold(task, view.rows, view.doc, view.graph, view.fix) or "it is not ready"
    return "" if view.admits(task) else "a release or apply wait holds it"


def take(queue: WorkQueue, view: Snapshot | Callable[[], Snapshot | None], timeout_ms: int) -> Delivery | None:
    """A view that yields no snapshot leaves the recheck to the hub, which admits the offer."""
    while (delivery := queue.take(timeout_ms)) is not None:
        current = view() if callable(view) else view
        if current is None or not withdrawal(delivery.event, current):
            return delivery
        delivery.term()
    return None
