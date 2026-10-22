import pytest

from scripts.events import memory
from scripts.events.config import BusConfig, QueuePolicy
from scripts.swarm import readiness
from scripts.swarm_ledger.repository import hierarchy

pytestmark = pytest.mark.unit

SLUG = "sw"
NOW = 5_000


def task(task_id, phase="p1", **fields):
    return {"id": task_id, "title": f"title {task_id}", "phase": phase, "state": "open", "lane": "eng", **fields}


def doc(tasks, **extra):
    phases = [
        {"id": "p1", "title": "build", "depends_on": []},
        {"id": "p2", "title": "later", "depends_on": ["p1"]},
    ]
    return {"overview": "o", "tasks": list(tasks), "phases": phases, **extra}


def snapshot(found, claimed=(), fix="", admits=lambda task: True):
    rows = {t["id"]: t for t in found["tasks"]}
    graph = hierarchy.project(found)[0]
    return readiness.Snapshot(SLUG, rows, found, graph, fix, lambda task_id: task_id in claimed, admits)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def bus(clock):
    return memory.open_bus(BusConfig(), clock=clock)


def queued(bus, lane, view):
    taken = []
    while (delivery := readiness.take(bus.queue(readiness.queue_name(SLUG, lane), QueuePolicy()), view, 0)) is not None:
        delivery.ack()
        taken.append(delivery.event.key)
    return taken


def test_ready_is_open_in_scope_unmerged_and_unheld():
    found = doc(
        [
            task("a"),
            task("b", phase="p2"),
            task("c", state="claimed"),
            task("d", out_of_scope=True),
            task("e", merged_into="a"),
            task("f", depends_on=["c"]),
            task("g", state="done"),
        ]
    )
    view = snapshot(found)
    assert [t for t in view.rows if readiness.ready(view.rows[t], view.rows, found, view.graph, "")] == ["a"]


def test_claimable_needs_the_lane_readiness_and_no_claim():
    found = doc([task("a"), task("b", lane="ci"), task("c"), task("d", state="claimed"), task("e", state="done")])
    view = snapshot(found, claimed={"c"})
    assert [t for t in view.rows if readiness.claimable(view.rows[t], "eng", view)] == ["a"]
    assert [t for t in view.rows if readiness.claimable(view.rows[t], "ci", view)] == ["b"]


def test_claimable_needs_the_tick_admission_for_release_and_apply_waits():
    seen = []
    view = snapshot(doc([task("a"), task("b")]), admits=lambda t: seen.append(t["id"]) or t["id"] == "b")
    assert [t for t in view.rows if readiness.claimable(view.rows[t], "eng", view)] == ["b"]
    assert seen == ["a", "b"]


def test_a_snapshot_cannot_be_changed():
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        snapshot(doc([])).fix = "p1"


def test_the_offer_id_names_the_swarm_task_and_claim_generation():
    assert readiness.offer_id(SLUG, task("a")) == "ready:sw:a:0"
    assert readiness.offer_id(SLUG, task("a", claim_generation=3)) == "ready:sw:a:3"
    assert readiness.offer_id("other", task("b", claim_generation=1)) == "ready:other:b:1"


def test_each_lane_has_its_own_work_queue():
    assert readiness.queue_name(SLUG, "eng") == "swarm.sw.ready.eng"
    assert readiness.queue_name("other", "ci") == "swarm.other.ready.ci"


def test_an_offer_carries_the_task_lane_and_generation():
    view = snapshot(doc([task("a", lane="ci", claim_generation=2)]))
    event = readiness.offer(view.rows["a"], view, NOW)
    assert event.to_dict() == {
        "id": "ready:sw:a:2",
        "type": "task.ready",
        "swarm": SLUG,
        "entity": "task",
        "key": "a",
        "version": 2,
        "time": NOW,
        "author": "readiness",
        "data": {"lane": "ci", "generation": 2},
        "cause": None,
        "schema_version": 1,
    }


def test_offers_cover_every_ready_task_in_its_own_lane_and_skip_frozen_claimed_and_laneless():
    found = doc(
        [
            task("a"),
            task("b", lane="ci"),
            task("frozen"),
            task("claimed"),
            task("held", state="claimed"),
            task("laneless", lane=""),
            task("plan", lane="plan", phase="p2"),
        ],
        freezes=[{"verb": "freeze", "target": "tasks/frozen"}],
    )
    view = snapshot(found, claimed={"claimed"})
    assert [(event.key, event.data["lane"]) for event in readiness.offers(view, NOW)] == [("a", "eng"), ("b", "ci")]


def test_publish_offers_each_ready_task_to_its_lane_queue_once_per_generation(bus):
    found = doc([task("a"), task("b", lane="ci")])
    view = snapshot(found)
    assert readiness.publish(bus, view, NOW) == ["ready:sw:a:0", "ready:sw:b:0"]
    assert readiness.publish(bus, view, NOW) == []
    assert queued(bus, "eng", view) == ["a"]
    assert queued(bus, "ci", view) == ["b"]


def test_a_new_claim_generation_is_offered_again(bus):
    readiness.publish(bus, snapshot(doc([task("a")])), NOW)
    reopened = snapshot(doc([task("a", claim_generation=1)]))
    assert readiness.publish(bus, reopened, NOW) == ["ready:sw:a:1"]
    assert queued(bus, "eng", reopened) == ["a"]


def test_an_unfrozen_task_is_offered_again_once_the_dedupe_window_from_its_first_offer_passes(bus, clock):
    found = doc([task("a")])
    readiness.publish(bus, snapshot(found), NOW)
    frozen = snapshot(doc([task("a")], freezes=[{"verb": "freeze", "target": "tasks/a"}]))
    queue = bus.queue(readiness.queue_name(SLUG, "eng"), QueuePolicy())
    assert readiness.take(queue, frozen, 0) is None
    clock.now += BusConfig().dedupe_window_ms - 1
    assert readiness.publish(bus, snapshot(found), clock.now) == []
    clock.now += 1
    assert readiness.publish(bus, snapshot(found), clock.now) == ["ready:sw:a:0"]
    assert queued(bus, "eng", snapshot(found)) == ["a"]


def test_publish_uses_the_given_queue_policy(bus):
    policy = QueuePolicy(max_deliver=2)
    readiness.publish(bus, snapshot(doc([task("a")])), NOW, policy)
    assert bus.queue(readiness.queue_name(SLUG, "eng"), policy).policy == policy
    with pytest.raises(ValueError, match="already has another policy"):
        bus.queue(readiness.queue_name(SLUG, "eng"), QueuePolicy())


def test_a_live_offer_has_no_withdrawal():
    view = snapshot(doc([task("a", claim_generation=1)]))
    assert readiness.withdrawal(readiness.offer(view.rows["a"], view, NOW), view) == ""


def test_an_offer_is_withdrawn_once_its_task_is_frozen_claimed_moved_or_gone():
    offered = snapshot(doc([task("a")]))
    event = readiness.offer(offered.rows["a"], offered, NOW)
    frozen = doc([task("a")], freezes=[{"verb": "freeze", "target": "tasks/a"}])
    assert readiness.withdrawal(event, snapshot(frozen)) == "the freeze on task title a holds it"
    assert readiness.withdrawal(event, snapshot(doc([task("a")]), claimed={"a"})) == "it is claimed"
    assert readiness.withdrawal(event, snapshot(doc([task("a", state="claimed")]))) == "it is claimed"
    assert readiness.withdrawal(event, snapshot(doc([task("a", claim_generation=1)]))) == "it is at generation 1"
    assert readiness.withdrawal(event, snapshot(doc([task("a", lane="ci")]))) == "it moved to lane ci"
    assert readiness.withdrawal(event, snapshot(doc([task("b")]))) == "the task is gone"
    assert readiness.withdrawal(event, snapshot(doc([task("a", state="done")]))) == "it is done"
    assert readiness.withdrawal(event, snapshot(doc([task("a", out_of_scope=True)]))) == "it is not ready"
    held = snapshot(doc([task("a")]), admits=lambda t: False)
    assert readiness.withdrawal(event, held) == "a release or apply wait holds it"


def test_take_terminates_withdrawn_offers_and_returns_the_first_live_one(bus, clock):
    found = doc([task("a"), task("b"), task("c")])
    readiness.publish(bus, snapshot(found), NOW)
    now = snapshot(
        doc([task("a", state="claimed"), task("b"), task("c")], freezes=[{"verb": "freeze", "target": "tasks/b"}])
    )
    queue = bus.queue(readiness.queue_name(SLUG, "eng"), QueuePolicy())
    delivery = readiness.take(queue, now, 0)
    assert (delivery.event.key, delivery.attempt) == ("c", 1)
    delivery.ack()
    clock.now += QueuePolicy().ack_wait_ms
    assert readiness.take(queue, snapshot(found), 0) is None


def test_take_on_an_empty_queue_returns_none(bus):
    queue = bus.queue(readiness.queue_name(SLUG, "eng"), QueuePolicy())
    assert readiness.take(queue, snapshot(doc([task("a")])), 0) is None


def test_take_passes_its_timeout_to_the_queue():
    seen = []

    class Queue:
        def take(self, timeout_ms):
            seen.append(timeout_ms)

    assert readiness.take(Queue(), snapshot(doc([])), 250) is None
    assert seen == [250]
