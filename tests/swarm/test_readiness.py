import itertools
import json
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from scripts.events import memory
from scripts.events.bus import Bus, WorkQueue
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope
from scripts.inbox.store import InboxStore
from scripts.swarm import freeze, preempt, readiness
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
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


WINDOW = BusConfig().dedupe_window_ms
LATER = NOW + WINDOW
ORDER = {"at": 1, "by": "operator"}


def offer_aged(view: readiness.Snapshot, task_id: str, age: int = WINDOW) -> Envelope:
    return readiness.offer(view.rows[task_id], view, LATER - age)


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


def test_publish_opens_every_lane_queue_even_with_no_offer_so_the_plan_stream_exists(bus: Bus) -> None:
    opened = []
    real = bus.queue

    def queue(name: str, policy: QueuePolicy) -> WorkQueue:
        opened.append((name, policy))
        return real(name, policy)

    bus.queue = queue
    policy = QueuePolicy(max_deliver=3)
    assert readiness.publish(bus, snapshot(doc([])), NOW, policy) == []
    assert opened == [(readiness.queue_name(SLUG, lane), policy) for lane in ("eng", "ci", "review", "plan")]


def test_publish_offers_a_plan_task_to_the_plan_queue_it_opened(bus: Bus) -> None:
    found = doc([task("p", lane="plan")])
    assert readiness.publish(bus, snapshot(found), NOW) == ["ready:sw:p:0"]
    assert queued(bus, "plan", snapshot(found)) == ["p"]
    assert queued(bus, "eng", snapshot(found)) == []


def test_a_new_claim_generation_is_offered_again(bus):
    readiness.publish(bus, snapshot(doc([task("a")])), NOW)
    reopened = snapshot(doc([task("a", claim_generation=1)]))
    assert readiness.publish(bus, reopened, NOW) == ["ready:sw:a:1"]
    assert queued(bus, "eng", reopened) == ["a"]


@pytest.mark.feature("unfit-refusal-spends-no-delivery")
def test_an_offer_still_queued_past_the_dedupe_window_is_not_offered_again(bus: Bus, clock: Clock) -> None:
    view = snapshot(doc([task("a")]))
    assert readiness.publish(bus, view, NOW) == ["ready:sw:a:0"]
    clock.now += BusConfig().dedupe_window_ms
    assert readiness.publish(bus, view, clock.now) == []
    assert queued(bus, "eng", view) == ["a"]


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


@pytest.mark.feature("ready-urgent-offer-precedes-older-normal-offers")
def test_a_later_urgent_offer_is_taken_before_older_normal_offers_queued_past_the_dedupe_window(
    bus: Bus, clock: Clock
) -> None:
    readiness.publish(bus, snapshot(doc([task("n1"), task("n2")])), NOW)
    clock.now += BusConfig().dedupe_window_ms
    view = snapshot(doc([task("n1"), task("n2"), task("u", rank="urgent")]))
    readiness.publish(bus, view, clock.now)
    assert queued(bus, "eng", view) == ["u", "n1", "n2"]


@pytest.mark.feature("ready-urgent-offer-precedes-older-normal-offers")
def test_a_queued_task_that_turned_urgent_is_taken_before_the_older_normal_offer_ahead_of_it(
    bus: Bus, clock: Clock
) -> None:
    readiness.publish(bus, snapshot(doc([task("n1"), task("u")])), NOW)
    clock.now += BusConfig().dedupe_window_ms
    view = snapshot(doc([task("n1"), task("u", rank="urgent")]))
    assert readiness.publish(bus, view, clock.now) == ["ready:sw:n1:0"]
    assert queued(bus, "eng", view) == ["u", "n1"]


@pytest.mark.feature("ready-urgent-offer-precedes-older-normal-offers")
def test_a_normal_offer_younger_than_the_dedupe_window_keeps_its_place_ahead_of_a_later_urgent_offer(
    bus: Bus, clock: Clock
) -> None:
    readiness.publish(bus, snapshot(doc([task("n1")])), NOW)
    clock.now += BusConfig().dedupe_window_ms - 1
    view = snapshot(doc([task("n1"), task("u", rank="urgent")]))
    assert readiness.publish(bus, view, clock.now) == ["ready:sw:u:0"]
    assert queued(bus, "eng", view) == ["n1", "u"]


@pytest.mark.feature("ready-urgent-offer-precedes-older-normal-offers")
def test_a_hive_that_cannot_place_the_urgent_offer_still_takes_the_older_normal_offer(bus: Bus, clock: Clock) -> None:
    readiness.publish(bus, snapshot(doc([task("n1")])), NOW)
    clock.now += BusConfig().dedupe_window_ms
    view = snapshot(doc([task("n1"), task("u", rank="urgent", hive="other")]))
    readiness.publish(bus, view, clock.now)
    queue = bus.queue(readiness.queue_name(SLUG, "eng"), QueuePolicy())
    delivery = readiness.take(queue, view, 0, lambda event: readiness.placeable(event.data, "mine", lambda: []))
    assert delivery is not None and delivery.event.key == "n1"


@pytest.mark.feature("ready-urgent-offer-precedes-older-normal-offers")
def test_an_urgent_offer_in_another_lane_leaves_the_older_normal_offers_in_place(bus: Bus, clock: Clock) -> None:
    readiness.publish(bus, snapshot(doc([task("n1")])), NOW)
    clock.now += BusConfig().dedupe_window_ms
    view = snapshot(doc([task("n1"), task("u", lane="ci", rank="urgent")]))
    assert readiness.publish(bus, view, clock.now) == ["ready:sw:u:0"]
    assert queued(bus, "eng", view) == ["n1"]


@pytest.mark.feature("ready-urgent-offer-precedes-older-normal-offers")
def test_the_seat_a_preempted_normal_worker_frees_goes_to_the_urgent_offer_not_the_older_normal_one(
    bus: Bus, clock: Clock
) -> None:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=1))
    readiness.publish(bus, snapshot(doc([task("n1")])), NOW)
    clock.now += BusConfig().dedupe_window_ms
    held, urgent = task("n0", state="claimed"), task("u", rank="urgent")
    found = doc([held, urgent])
    full = {"eng": ([urgent], [AgentRecord("e1", "eng", "n0")])}
    preempt.step(SLUG, store, {"n0": held, "u": urgent}, found, full, 1)
    preempt.step(SLUG, store, {"n0": held, "u": urgent}, found, full, 2)
    assert [item.task for item in InboxStore(store.redis).open_items("e1")] == ["n0"]
    view = snapshot(doc([task("n1"), urgent]))
    readiness.publish(bus, view, clock.now)
    delivery = readiness.take(bus.queue(readiness.queue_name(SLUG, "eng"), QueuePolicy()), view, 0)
    assert delivery is not None and delivery.event.key == "u"


@pytest.mark.feature("ready-urgent-offer-precedes-older-normal-offers")
def test_behind_names_the_old_waiting_offers_ahead_of_an_urgent_or_focused_offer() -> None:
    focus = [{"verb": "focus", "target": "tasks/f"}]
    view = snapshot(doc([task("n1"), task("n2"), task("u", rank="urgent"), task("f")], freezes=focus))
    n1, n2, u, f = (offer_aged(view, name) for name in ("n1", "n2", "u", "f"))
    assert readiness.behind([n1, offer_aged(view, "n2", WINDOW - 1)], [offer_aged(view, "u", 0)], view, LATER) == {
        n1.id
    }
    assert readiness.behind([n1, u, n2], [], view, LATER) == {n1.id}
    assert readiness.behind([n1, n2], [offer_aged(view, "f", 0)], view, LATER) == {n1.id, n2.id}
    assert readiness.behind([u, n1], [offer_aged(view, "f", 0)], view, LATER) == {n1.id}
    assert readiness.behind([u, f, n1, n2], [], view, LATER) == set()
    assert readiness.behind([n1, n2], [], view, LATER) == set()
    assert readiness.behind([], [offer_aged(view, "u", 0)], view, LATER) == set()


@pytest.mark.feature("ready-ordered-offer-first")
def test_behind_names_the_old_waiting_offers_ahead_of_an_offer_of_a_higher_class() -> None:
    later = {"at": 9, "by": "operator"}
    view = snapshot(doc([task("n"), task("o", ordered=ORDER), task("o2", ordered=later), task("u", rank="urgent")]))
    n, o, o2, u = (offer_aged(view, name) for name in ("n", "o", "o2", "u"))
    assert readiness.behind([n, o], [], view, LATER) == {n.id}
    assert readiness.behind([n, o, u], [], view, LATER) == {n.id, o.id}
    assert readiness.behind([o, n], [offer_aged(view, "u", 0)], view, LATER) == {o.id, n.id}
    assert readiness.behind([o], [offer_aged(view, "n", 0), offer_aged(view, "u", 0)], view, LATER) == {o.id}
    assert readiness.behind([o2, n], [offer_aged(view, "o", 0)], view, LATER) == {n.id}
    assert readiness.behind([o2], [offer_aged(view, "o", 0)], view, LATER) == set()
    assert readiness.behind([offer_aged(view, "n", WINDOW - 1), o], [], view, LATER) == set()
    assert readiness.behind([u, o, n], [], view, LATER) == set()


@pytest.mark.feature("ready-ordered-offer-first")
def test_behind_moves_a_waiting_offer_of_the_rest_behind_a_later_offer_of_a_higher_rank() -> None:
    view = snapshot(doc([task("n"), task("h", rank="high"), task("l", rank="low")]))
    n, h, low = (offer_aged(view, name) for name in ("n", "h", "l"))
    assert readiness.behind([n, h], [], view, LATER) == {n.id}
    assert readiness.behind([low, n, h], [], view, LATER) == {low.id, n.id}
    assert readiness.behind([low], [offer_aged(view, "n", 0)], view, LATER) == {low.id}
    assert readiness.behind([n, low], [offer_aged(view, "h", 0)], view, LATER) == {n.id, low.id}
    assert readiness.behind([h, n, low], [], view, LATER) == set()
    assert readiness.behind([h], [offer_aged(view, "n", 0)], view, LATER) == set()
    assert readiness.behind([offer_aged(view, "n", WINDOW - 1), h], [], view, LATER) == set()


@pytest.mark.feature("ready-ordered-offer-first")
def test_behind_moves_a_waiting_ordered_offer_behind_a_later_one_ordered_before_it_or_ranked_above_it() -> None:
    later = {"at": 9, "by": "operator"}
    tasks = [
        task("o", ordered=ORDER),
        task("oh", ordered=ORDER, rank="high"),
        task("o2", ordered=later),
        task("o2h", ordered=later, rank="high"),
    ]
    view = snapshot(doc(tasks))
    o, oh, o2, o2h = (offer_aged(view, name) for name in ("o", "oh", "o2", "o2h"))
    assert readiness.behind([o2, o], [], view, LATER) == {o2.id}
    assert readiness.behind([o2h, o], [], view, LATER) == {o2h.id}
    assert readiness.behind([o, oh], [], view, LATER) == {o.id}
    assert readiness.behind([o2], [offer_aged(view, "o2h", 0)], view, LATER) == {o2.id}
    assert readiness.behind([oh, o, o2h, o2], [], view, LATER) == set()
    assert readiness.behind([offer_aged(view, "o2", WINDOW - 1), o], [], view, LATER) == set()


@pytest.mark.feature("ready-ordered-offer-first")
def test_behind_moves_nothing_inside_the_urgent_or_focused_class() -> None:
    focus = [{"verb": "focus", "target": "tasks/f"}, {"verb": "focus", "target": "tasks/fh"}]
    tasks = [task("u", rank="urgent"), task("f"), task("fh", rank="high"), task("fl", rank="low")]
    view = snapshot(doc(tasks, freezes=[*focus, {"verb": "focus", "target": "tasks/fl"}]))
    u, f, fh, fl = (offer_aged(view, name) for name in ("u", "f", "fh", "fl"))
    assert readiness.behind([f, u], [], view, LATER) == set()
    assert readiness.behind([u, f], [], view, LATER) == set()
    assert readiness.behind([fl, f, fh, u], [], view, LATER) == set()
    assert readiness.behind([fl], [offer_aged(view, "u", 0), offer_aged(view, "fh", 0)], view, LATER) == set()


@pytest.mark.feature("ready-ordered-offer-first")
def test_a_queued_task_raised_to_high_is_taken_before_the_older_normal_offers_ahead_of_it_and_moves_them_once(
    bus: Bus, clock: Clock
) -> None:
    readiness.publish(bus, snapshot(doc([task("n1"), task("n2"), task("h")])), NOW)
    clock.now += WINDOW
    view = snapshot(doc([task("n1"), task("n2"), task("h", rank="high")]))
    assert readiness.publish(bus, view, clock.now) == ["ready:sw:n1:0", "ready:sw:n2:0"]
    clock.now += WINDOW
    assert readiness.publish(bus, view, clock.now) == []
    assert queued(bus, "eng", view) == ["h", "n1", "n2"]


@pytest.mark.feature("ready-ordered-offer-first")
def test_a_queued_task_lowered_to_low_is_taken_after_the_normal_offers_behind_it(bus: Bus, clock: Clock) -> None:
    readiness.publish(bus, snapshot(doc([task("l"), task("n1"), task("n2")])), NOW)
    clock.now += WINDOW
    view = snapshot(doc([task("l", rank="low"), task("n1"), task("n2")]))
    assert readiness.publish(bus, view, clock.now) == ["ready:sw:l:0"]
    assert queued(bus, "eng", view) == ["n1", "n2", "l"]


@pytest.mark.feature("ready-ordered-offer-first")
def test_an_ordered_offer_moves_behind_a_later_offer_whose_order_is_older(bus: Bus, clock: Clock) -> None:
    newer = task("o9", ordered={"at": 9, "by": "operator"})
    readiness.publish(bus, snapshot(doc([newer])), NOW)
    clock.now += WINDOW
    view = snapshot(doc([newer, task("o1", ordered=ORDER)]))
    assert readiness.publish(bus, view, clock.now) == ["ready:sw:o1:0", "ready:sw:o9:0"]
    assert queued(bus, "eng", view) == ["o1", "o9"]


@pytest.mark.feature("ready-ordered-offer-first")
def test_offers_put_urgent_tasks_first_then_ordered_ones_oldest_order_first_then_the_rest() -> None:
    found = doc(
        [
            task("n"),
            task("h", rank="high"),
            task("o5", ordered={"at": 5, "by": "operator"}),
            task("o3", ordered={"at": 3, "by": "operator"}),
            task("o3h", ordered={"at": 3, "by": "operator"}, rank="high"),
            task("u", rank="urgent"),
            task("ou", ordered=ORDER, rank="urgent"),
        ]
    )
    assert [event.key for event in readiness.offers(snapshot(found), NOW)] == ["u", "ou", "o3h", "o3", "o5", "h", "n"]


@pytest.mark.feature("ready-ordered-offer-first")
def test_the_tasks_of_an_ordered_phase_and_the_work_they_wait_on_are_offered_before_the_rest() -> None:
    phases = [{"id": "p1", "depends_on": []}, {"id": "p2", "depends_on": [], "ordered": ORDER}]
    tasks = [task("n"), task("dep"), task("held", phase="p2", depends_on=["dep"]), task("o", phase="p2")]
    found = {**doc(tasks), "phases": phases}
    assert [event.key for event in readiness.offers(snapshot(found), NOW)] == ["dep", "o", "n"]


@pytest.mark.feature("ready-ordered-offer-first")
def test_order_marks_change_the_order_of_offers_and_never_which_tasks_are_offered() -> None:
    def offered(mark: dict[str, Any]) -> list[str]:
        tasks = [
            task("a"),
            task("b", lane="ci", **mark),
            task("dep", **mark),
            task("waits", depends_on=["dep"], **mark),
            task("frozen", **mark),
            task("claimed", **mark),
            task("done", state="done", **mark),
            task("z", **mark),
        ]
        found = doc(tasks, freezes=[{"verb": "freeze", "target": "tasks/frozen"}])
        return [event.key for event in readiness.offers(snapshot(found, claimed={"claimed"}), NOW)]

    assert offered({}) == ["a", "b", "dep", "z"]
    assert offered({"ordered": ORDER}) == ["b", "dep", "z", "a"]


@pytest.mark.feature("ready-ordered-offer-first")
def test_under_a_focus_an_ordered_task_outside_it_is_not_offered_and_focused_offers_keep_their_order() -> None:
    focus = [{"verb": "focus", "target": "tasks/f2"}, {"verb": "focus", "target": "tasks/f1"}]
    found = doc([task("o", ordered=ORDER), task("f1", ordered=ORDER), task("f2"), task("n")], freezes=focus)
    assert [event.key for event in readiness.offers(snapshot(found), NOW)] == ["f2", "f1"]


@pytest.mark.feature("ready-ordered-offer-first")
def test_a_malformed_order_mark_leaves_the_offers_in_their_rank_order() -> None:
    found = doc([task("a"), task("b", ordered="yes"), task("c", ordered={"by": "operator"}), task("h", rank="high")])
    assert [event.key for event in readiness.offers(snapshot(found), NOW)] == ["h", "a", "b", "c"]


@pytest.mark.feature("ready-ordered-offer-first")
def test_a_later_ordered_offer_is_taken_after_an_urgent_one_and_before_older_normal_offers(
    bus: Bus, clock: Clock
) -> None:
    readiness.publish(bus, snapshot(doc([task("n1"), task("n2")])), NOW)
    clock.now += WINDOW
    view = snapshot(doc([task("n1"), task("n2"), task("o", ordered=ORDER), task("u", rank="urgent")]))
    placed = ["ready:sw:u:0", "ready:sw:o:0", "ready:sw:n1:0", "ready:sw:n2:0"]
    assert readiness.publish(bus, view, clock.now) == placed
    assert readiness.publish(bus, view, clock.now) == []
    assert queued(bus, "eng", view) == ["u", "o", "n1", "n2"]


@pytest.mark.feature("ready-ordered-offer-first")
def test_an_old_normal_offer_moves_behind_a_later_ordered_offer_and_a_young_one_keeps_its_place(
    bus: Bus, clock: Clock
) -> None:
    readiness.publish(bus, snapshot(doc([task("n1")])), NOW)
    clock.now += WINDOW - 1
    readiness.publish(bus, snapshot(doc([task("n1"), task("n2")])), clock.now)
    clock.now += 1
    view = snapshot(doc([task("n1"), task("n2"), task("o", ordered=ORDER)]))
    assert readiness.publish(bus, view, clock.now) == ["ready:sw:o:0", "ready:sw:n1:0"]
    assert queued(bus, "eng", view) == ["n2", "o", "n1"]


@pytest.mark.feature("ready-ordered-offer-first")
def test_an_ordered_offer_moves_behind_a_later_urgent_offer_and_stays_ahead_of_a_later_normal_one(
    bus: Bus, clock: Clock
) -> None:
    ordered = task("o", ordered=ORDER)
    readiness.publish(bus, snapshot(doc([ordered])), NOW)
    clock.now += WINDOW
    view = snapshot(doc([ordered, task("n")]))
    assert readiness.publish(bus, view, clock.now) == ["ready:sw:n:0"]
    clock.now += WINDOW
    view = snapshot(doc([ordered, task("n"), task("u", rank="urgent")]))
    assert readiness.publish(bus, view, clock.now) == ["ready:sw:u:0", "ready:sw:o:0", "ready:sw:n:0"]
    assert queued(bus, "eng", view) == ["u", "o", "n"]


@pytest.mark.feature("ready-ordered-offer-first")
def test_an_ordered_offer_younger_than_the_dedupe_window_keeps_its_place_ahead_of_an_urgent_one_until_it_ages(
    bus: Bus, clock: Clock
) -> None:
    ordered = task("o", ordered=ORDER)
    readiness.publish(bus, snapshot(doc([ordered])), NOW)
    clock.now += WINDOW - 1
    view = snapshot(doc([ordered, task("u", rank="urgent")]))
    assert readiness.publish(bus, view, clock.now) == ["ready:sw:u:0"]
    clock.now += 1
    assert readiness.publish(bus, view, clock.now) == ["ready:sw:o:0"]
    assert queued(bus, "eng", view) == ["u", "o"]


def focused(target: str, outside: dict[str, str]) -> readiness.Snapshot:
    tasks = [
        task("b", kind="code"),
        task("a", kind="code", ordered=ORDER),
        task("w", kind="code", depends_on=["d"]),
        task("d", ordered=ORDER, **outside),
    ]
    phases = [{"id": "p1", "title": "build", "depends_on": []}, {"id": "p2", "title": "later", "depends_on": []}]
    return snapshot(doc(tasks, phases=phases, freezes=[{"verb": "focus", "target": target}]))


@pytest.mark.feature("ready-ordered-offer-first")
@pytest.mark.parametrize(
    ("target", "outside"),
    [("phases/p1", {"phase": "p2"}), ("lane:eng", {"lane": "ci"}), ("kind:code", {"kind": "ops"})],
)
def test_a_focus_of_any_form_puts_its_tasks_in_the_first_class_of_offers(target: str, outside: dict[str, str]) -> None:
    assert [event.key for event in readiness.offers(focused(target, outside), NOW)] == ["b", "a", "d"]


@pytest.mark.feature("ready-ordered-offer-first")
@pytest.mark.parametrize(("target", "outside"), [("phases/p1", {"phase": "p2"}), ("kind:code", {"kind": "ops"})])
def test_an_ordered_offer_a_focus_lets_through_moves_behind_a_later_offer_of_the_focus(
    target: str, outside: dict[str, str]
) -> None:
    view = focused(target, outside)
    waited = offer_aged(view, "d")
    assert readiness.behind([waited], [offer_aged(view, "a", 0)], view, LATER) == {waited.id}


SELECTED = {"lane": ("lane:eng", {"lane": "ci"}), "kind": ("kind:code", {"kind": "ops"})}


@pytest.mark.feature("ready-offer-class-one-decider")
def test_the_selector_cases_name_exactly_the_focus_selectors_that_exist() -> None:
    assert set(SELECTED) == set(freeze.SELECTORS)


@pytest.mark.feature("ready-offer-class-one-decider")
@pytest.mark.parametrize("selector", sorted(freeze.SELECTORS))
def test_offers_and_behind_class_a_task_under_a_focus_selector_alike(selector: str) -> None:
    target, outside = SELECTED[selector]
    view = focused(target, outside)
    assert [event.key for event in readiness.offers(view, NOW)] == ["b", "a", "d"]
    waited = offer_aged(view, "d")
    assert readiness.behind([waited], [offer_aged(view, "a", 0)], view, LATER) == {waited.id}
    assert readiness.behind([offer_aged(view, "a")], [offer_aged(view, "d", 0)], view, LATER) == set()


RECORDED = Path(__file__).with_name("offer_classes_recorded.json")
FOCUS_FORMS: tuple[tuple[str, ...], ...] = (
    (),
    ("phases/p1",),
    ("tasks/t5",),
    ("lane:eng",),
    ("kind:code",),
    ("kind:ops", "phases/p2"),
    ("tasks/t11", "lane:ci"),
    ("lane:",),
    ("phase:p1",),
    ("tasks/t4", "tasks/t4", "kind:ops"),
    ("phases/p1", "freeze tasks/t4"),
    ("freeze phases/p2",),
)


def generated(forms: tuple[str, ...], plain: bool = False) -> readiness.Snapshot:
    """A ledger of twelve tasks that vary lane, kind, phase, rank and order mark, three of them waiting on another,
    under the given freeze records: a form is a focus target, or `freeze` and a target. Plain, the tasks carry no
    rank and no order mark."""
    marks = (None, {"at": 3, "by": "operator"}, {"at": 7, "by": "operator"})
    waits = {1: ["t10"], 9: ["t6"], 0: ["t7"]}
    tasks = [
        task(
            f"t{index}",
            phase=("p1", "p2")[index % 2],
            lane=("eng", "ci")[index // 2 % 2],
            kind=("code", "ops")[index // 4 % 2],
            depends_on=waits.get(index, []),
            **({"rank": rank} if rank and not plain else {}),
            **({"ordered": mark} if mark and not plain else {}),
        )
        for index, (rank, mark) in enumerate(itertools.product((None, "urgent", "high", "low"), marks))
    ]
    records = [
        {"verb": "freeze", "target": form.removeprefix("freeze ")}
        if form.startswith("freeze ")
        else {"verb": "focus", "target": form}
        for form in forms
    ]
    phases = [{"id": "p1", "title": "build", "depends_on": []}, {"id": "p2", "title": "later", "depends_on": []}]
    return snapshot(doc(tasks, phases=phases, freezes=records))


def recorded_run() -> dict[str, dict[str, list[str]]]:
    """For each set of freeze records: the offers in order, the waiting offers `behind` moves, and what one pass
    places and leaves queued in each lane after an earlier pass queued the same tasks plain."""
    found: dict[str, dict[str, list[str]]] = {}
    for forms in FOCUS_FORMS:
        view = generated(forms)
        keys = [event.key for event in readiness.offers(view, NOW)]
        waiting = [offer_aged(view, key) for key in reversed(keys)]
        fresh = [offer_aged(view, key, 0) for key in keys[1::2]]
        clock = Clock()
        bus = cast(Bus, memory.open_bus(BusConfig(), clock=clock))
        readiness.publish(bus, generated((), plain=True), clock.now)
        clock.now += WINDOW
        found[" ".join(forms) or "no focus"] = {
            "offers": keys,
            "behind with every offer waiting": sorted(readiness.behind(waiting, [], view, LATER)),
            "behind with every second offer new": sorted(readiness.behind(waiting[::2], fresh, view, LATER)),
            "placed after a plain pass": readiness.publish(bus, view, clock.now),
            "queued in eng": queued(bus, "eng", view),
            "queued in ci": queued(bus, "ci", view),
        }
    return found


@pytest.mark.feature("ready-offer-class-one-decider")
def test_a_recorded_run_of_generated_ledgers_keeps_its_offers_and_its_moves() -> None:
    assert recorded_run() == json.loads(RECORDED.read_text())


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


def test_withdrawal_judges_readiness_holds_and_admission_on_the_snapshot(monkeypatch):
    seen = []
    view = snapshot(doc([task("a")]), fix="f", admits=lambda t: seen.append(("admits", t)) or True)
    event = readiness.offer(view.rows["a"], view, NOW)
    monkeypatch.setattr(readiness, "ready", lambda *args: seen.append(("ready", args)) or False)
    monkeypatch.setattr(readiness.claim_hold, "hold", lambda *args: seen.append(("hold", args)) or "held")
    assert readiness.withdrawal(event, view) == "held"
    expected = (view.rows["a"], view.rows, view.doc, view.graph, "f")
    assert seen == [("ready", expected), ("hold", expected)]
    seen.clear()
    monkeypatch.setattr(readiness, "ready", lambda *args: True)
    assert readiness.withdrawal(event, view) == ""
    assert seen == [("admits", view.rows["a"])]


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


def test_take_with_no_snapshot_returns_the_first_offer_for_the_hub_to_check(bus):
    readiness.publish(bus, snapshot(doc([task("a"), task("b")])), NOW)
    queue = bus.queue(readiness.queue_name(SLUG, "eng"), QueuePolicy())
    delivery = readiness.take(queue, lambda: None, 0)
    assert delivery.event.key == "a"


@pytest.mark.feature("hub-shared-ledger-copy")
def test_threads_asking_for_one_new_revision_fetch_the_export_once() -> None:
    began, second = threading.Event(), threading.Event()

    class Ledger:
        def __init__(self) -> None:
            self.fetches = 0

        def metadata(self, slug: str) -> dict[str, Any]:
            return {"_meta": {"rev": 7}}

        def state(self, slug: str) -> dict[str, Any]:
            self.fetches += 1
            if self.fetches == 1:
                began.set()
                second.wait(0.5)
            else:
                second.set()
            return doc([task("t1")])

    ledger = Ledger()
    source = readiness.Source(ledger=cast(Any, ledger))
    reader = threading.Thread(target=source.state, args=(SLUG,))
    reader.start()
    assert began.wait(5)
    source.state(SLUG)
    reader.join()
    assert ledger.fetches == 1


@pytest.mark.feature("hub-shared-ledger-copy")
def test_one_swarms_fetch_never_waits_for_another_swarms_fetch() -> None:
    began, other = threading.Event(), threading.Event()
    waited: list[bool] = []

    class Ledger:
        def metadata(self, slug: str) -> dict[str, Any]:
            return {"_meta": {"rev": 7}}

        def state(self, slug: str) -> dict[str, Any]:
            if slug == "a":
                began.set()
                waited.append(other.wait(0.5))
            else:
                other.set()
            return doc([task("t1")])

    source = readiness.Source(ledger=cast(Any, Ledger()))
    reader = threading.Thread(target=source.state, args=("a",))
    reader.start()
    assert began.wait(5)
    source.state("b")
    reader.join()
    assert waited == [True]


class Exports:
    """A ledger that answers its revisions in turn and counts every export it serves."""

    def __init__(self, *revisions: int) -> None:
        self.revisions, self.fetches = iter(revisions), 0

    def metadata(self, slug: str) -> dict[str, Any]:
        return {"_meta": {"rev": next(self.revisions)}}

    def state(self, slug: str) -> dict[str, Any]:
        self.fetches += 1
        return doc([task("t1")])


@pytest.mark.feature("hub-shared-ledger-copy-revision")
def test_a_reader_handed_an_older_revision_keeps_the_newer_copy_and_fetches_nothing() -> None:
    ledger = Exports(7, 6, 7)
    source = readiness.Source(ledger=cast(Any, ledger))
    for _ in range(3):
        source.state(SLUG)
    assert ledger.fetches == 1


@pytest.mark.feature("hub-shared-ledger-copy-revision")
def test_a_reader_waiting_behind_a_fetch_reads_the_revision_current_at_its_turn() -> None:
    began, asked, release = threading.Event(), threading.Event(), threading.Event()
    got: list[dict[str, Any]] = []

    class Ledger:
        def __init__(self) -> None:
            self.rev, self.reads = 7, 0

        def metadata(self, slug: str) -> dict[str, Any]:
            self.reads += 1
            if self.reads == 2:
                asked.set()
            return {"_meta": {"rev": self.rev}}

        def state(self, slug: str) -> dict[str, Any]:
            rev = self.rev
            if rev == 7:
                began.set()
                release.wait(5)
            return doc([task(f"t{rev}")])

    ledger = Ledger()
    source = readiness.Source(ledger=cast(Any, ledger))
    first = threading.Thread(target=source.state, args=(SLUG,))
    first.start()
    assert began.wait(5)
    second = threading.Thread(target=lambda: got.append(source.state(SLUG)))
    second.start()
    asked.wait(0.5)
    ledger.rev = 8
    release.set()
    first.join()
    second.join()
    assert [row["id"] for row in got[0]["tasks"]] == ["t8"]


@pytest.mark.feature("hub-shared-ledger-copy-revision")
def test_a_readers_change_to_its_task_rows_never_reaches_the_shared_copy() -> None:
    ledger = Exports(7, 7)
    source = readiness.Source(ledger=cast(Any, ledger))
    source.state(SLUG)["tasks"][0].update(state="blocked")
    assert [row["state"] for row in source.state(SLUG)["tasks"]] == ["open"]
    assert ledger.fetches == 1
