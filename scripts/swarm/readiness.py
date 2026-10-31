"""Which tasks a lane may claim, offered to the lane's work queue on every hub pass.

An offer's id carries the task's claim generation, so the bus drops a repeat only inside its dedupe window.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from scripts.events import subjects
from scripts.events.bus import Bus, Delivery, WorkQueue
from scripts.events.config import QueuePolicy
from scripts.events.envelope import Envelope
from scripts.events.scope import LANES
from scripts.swarm import apply_wait, claim_hold, fleet_red, freeze, release_wait
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import EVENTS_ENGINE, RedisStore, SwarmError, connect
from scripts.swarm_ledger.repository import hierarchy

AUTHOR = "readiness"
UNFIT_NAK_MS = 5_000


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
        self._exported: dict[str, tuple[int, dict]] = {}

    def slugs(self) -> list[str]:
        self._store = self._store or connect()
        found = [slug for slug in self._store.slugs() if _on_events(self._store, slug)]
        self._exported = {slug: kept for slug, kept in self._exported.items() if slug in found}
        return found

    def state(self, slug: str) -> dict:
        rev = self._ledger.metadata(slug)["_meta"]["rev"]
        kept = self._exported.get(slug)
        if kept is None or kept[0] != rev:
            kept = self._exported[slug] = (rev, self._ledger.state(slug))
        return kept[1]

    def snapshot(self, slug: str) -> Snapshot:
        self._store = self._store or connect()
        config = self._store.config(slug)
        doc = self.state(slug)
        rows = {task["id"]: task for task in doc["tasks"]}
        owned = {agent.task for agent in self._store.agents(slug) if agent.state != "finished"}
        released = release_wait.checker(config.repo)
        applied = apply_wait.checker(config, rows)
        unshared = fleet_red.checker()
        return Snapshot(
            slug,
            rows,
            doc,
            hierarchy.project(doc)[0],
            freeze.fix_phase(config),
            lambda task_id: task_id in owned or self._store.claimant(slug, task_id) is not None,
            lambda task: config.state == "running" and released(task) and applied(task) and unshared(task),
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


def offer(task: dict[str, Any], view: Snapshot, now_ms: int) -> Envelope:
    data: dict[str, Any] = {"lane": task["lane"], "generation": generation(task)}
    if hive := task.get("hive"):
        data["hive"] = hive
    if needs := task.get("needs"):
        data["needs"] = list(needs)
    return Envelope(
        id=offer_id(view.slug, task),
        type="task.ready",
        swarm=view.slug,
        entity="task",
        key=task["id"],
        version=generation(task),
        time=now_ms,
        author=AUTHOR,
        data=data,
    )


def placeable(task: Mapping[str, Any], hive: str, capabilities: Callable[[], Iterable[str]]) -> bool:
    """A hive never takes a workstation task, a task pinned to another hive or one needing what it lacks; its
    capabilities are read only for a task that names needs."""
    needs: set[str] = set(task.get("needs") or ())
    return (
        not task.get("workstation")
        and task.get("hive") in (None, "", hive)
        and (not needs or needs <= set(capabilities()))
    )


def fits(event: Envelope, hive: str, capabilities: Callable[[], Iterable[str]]) -> bool:
    return placeable(cast("Mapping[str, Any]", event.data), hive, capabilities)


def declared(store: Any, hive: str) -> Callable[[], list[str]]:
    """The capabilities a hive registered in the store's Redis, read when called."""

    def capabilities() -> list[str]:
        from scripts.hive import registry

        record = cast("Mapping[str, list[str]] | None", registry.show(store.redis, hive)) or {}
        return list(record.get("capabilities", []))

    return capabilities


def anywhere(event: Envelope) -> bool:
    return True


def tick_launches(engine: str, task: Mapping[str, Any]) -> bool:
    """Under the events engine hives take every task but a workstation task, which the workstation tick launches."""
    return engine != EVENTS_ENGINE or bool(task.get("workstation"))


def tick_places(engine: str, rows: Mapping[str, Mapping[str, Any]]) -> bool:
    return engine != EVENTS_ENGINE or any(
        row.get("state") == "open" and tick_launches(engine, row) for row in rows.values()
    )


def offers(view: Snapshot, now_ms: int) -> list[Envelope]:
    rows = cast("Mapping[str, dict[str, Any]]", view.rows)
    return [
        offer(task, view, now_ms)
        for task in rows.values()
        if task.get("lane") and not task.get("workstation") and claimable(task, task["lane"], view)
    ]


def publish(bus: Bus, view: Snapshot, now_ms: int, policy: QueuePolicy = QueuePolicy()) -> list[str]:
    """Opens every lane queue first, so a hive polling a lane with no offer yet finds its stream provisioned."""
    events = offers(view, now_ms)
    lanes = dict.fromkeys([*LANES, *(event.data["lane"] for event in events)])
    queues = {lane: bus.queue(queue_name(view.slug, lane), policy) for lane in lanes}
    placed = []
    for event in events:
        if queues[event.data["lane"]].offer(event):
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


def take(
    queue: WorkQueue,
    view: Snapshot | Callable[[], Snapshot | None],
    timeout_ms: int,
    fit: Callable[[Envelope], bool] = anywhere,
) -> Delivery | None:
    """A view that yields no snapshot leaves the recheck to the hub, which admits the offer; an offer this hive may
    not take goes back to the queue for another hive."""
    while (delivery := queue.take(timeout_ms)) is not None:
        if not fit(delivery.event):
            delivery.nak(UNFIT_NAK_MS)
            continue
        current = view() if callable(view) else view
        if current is None or not withdrawal(delivery.event, current):
            return delivery
        delivery.term()
    return None
