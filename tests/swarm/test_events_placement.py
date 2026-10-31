"""Workstation, hive pinned and needs pinned tasks under the events engine: offers, spoke takes, hub admission, tick."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.memory import open_bus
from scripts.hive import lanes, registry, spoke
from scripts.hub import admission, slots
from scripts.swarm import readiness
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_readiness import NOW, SLUG, bus, clock, doc, snapshot, task  # noqa: F401
from tests.swarm.test_tick import FakeRuntime, store, tasks  # noqa: F401

pytestmark = pytest.mark.unit

PINNED = [
    task("plain"),
    task("desk", workstation=True),
    task("pinned", hive="hive-b"),
    task("gpu", needs=["gpu"]),
]


def no_capabilities() -> list[str]:
    raise AssertionError("an offer without needs reads no capabilities")


def test_a_workstation_task_is_never_offered_to_a_hive_queue() -> None:
    offered = readiness.offers(snapshot(doc(PINNED)), NOW)
    assert [event.key for event in offered] == ["plain", "pinned", "gpu"]


def test_an_offer_carries_its_hive_pin_and_needs_and_nothing_for_an_unpinned_task() -> None:
    offered = {event.key: event.data for event in readiness.offers(snapshot(doc(PINNED)), NOW)}
    assert offered == {
        "plain": {"lane": "eng", "generation": 0},
        "pinned": {"lane": "eng", "generation": 0, "hive": "hive-b"},
        "gpu": {"lane": "eng", "generation": 0, "needs": ["gpu"]},
    }


@pytest.mark.parametrize(
    "row, hive, capabilities, expected",
    [
        (task("plain"), "hive-a", (), True),
        (task("desk", workstation=True), "hive-a", (), False),
        (task("pinned", hive="hive-b"), "hive-a", (), False),
        (task("pinned", hive="hive-b"), "hive-b", (), True),
        (task("gpu", needs=["gpu"]), "hive-a", ("headed",), False),
        (task("gpu", needs=["gpu", "headed"]), "hive-a", ("headed", "gpu"), True),
    ],
)
def test_placeable_keeps_workstation_pinned_and_needs_tasks_off_hives_that_may_not_take_them(
    row: dict, hive: str, capabilities: tuple[str, ...], expected: bool
) -> None:
    assert readiness.placeable(row, hive, capabilities) is expected


def test_fits_reads_capabilities_only_for_an_offer_with_needs() -> None:
    offered = {event.key: event for event in readiness.offers(snapshot(doc(PINNED)), NOW)}
    assert readiness.fits(offered["plain"], "hive-a", no_capabilities)
    assert not readiness.fits(offered["pinned"], "hive-a", no_capabilities)
    assert readiness.fits(offered["pinned"], "hive-b", no_capabilities)
    assert not readiness.fits(offered["gpu"], "hive-a", lambda: ["headed"])
    assert readiness.fits(offered["gpu"], "hive-a", lambda: ["gpu"])


def test_a_hive_leaves_an_offer_pinned_elsewhere_in_the_queue_for_its_own_hive(bus, clock) -> None:  # noqa: F811
    readiness.publish(bus, snapshot(doc([task("pinned", hive="hive-b"), task("plain")])), NOW)
    queue = bus.queue(readiness.queue_name(SLUG, "eng"), QueuePolicy())

    taken = readiness.take(queue, lambda: None, 0, lambda event: readiness.fits(event, "hive-a", no_capabilities))
    assert taken.event.key == "plain"
    taken.ack()
    assert (
        readiness.take(queue, lambda: None, 0, lambda event: readiness.fits(event, "hive-a", no_capabilities)) is None
    )

    clock.now += readiness.UNFIT_NAK_MS
    held = readiness.take(queue, lambda: None, 0, lambda event: readiness.fits(event, "hive-b", no_capabilities))
    assert held.event.key == "pinned"


def test_a_skipped_offer_is_released_for_another_hive_not_terminated() -> None:
    delivery = SimpleNamespace(event=SimpleNamespace(key="pinned"), nak=Mock(), term=Mock())
    queue = SimpleNamespace(take=Mock(side_effect=[delivery, None]))

    assert readiness.take(queue, lambda: None, 0, lambda event: False) is None

    delivery.nak.assert_called_once_with(readiness.UNFIT_NAK_MS)
    delivery.term.assert_not_called()


@pytest.fixture
def rig():
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    swarm = RedisStore(redis)
    swarm.create(SwarmConfig(SLUG, "/repo", 2, 2))
    found = doc(PINNED)
    rows = {row["id"]: row for row in found["tasks"]}

    def update_task(slug, task_id, fields, if_state=(), if_match=None):
        row = rows[task_id]
        if row["state"] in if_state:
            row["claim_generation"] = row.get("claim_generation", 0) + 1
            row.update(fields)
        return dict(row)

    ledger = SimpleNamespace(update_task=update_task)
    source = SimpleNamespace(
        snapshot=lambda slug: snapshot(found, claimed=[t for t in rows if swarm.claimant(slug, t)])
    )
    yield SimpleNamespace(store=swarm, redis=redis, admitter=admission.Admission(swarm, ledger, source, lambda: 7_000))
    redis.close()


def ask(task_id: str, hive: str) -> dict:
    return {"hive": hive, "task": task_id, "lane": "eng", "generation": 0}


def test_the_hub_never_admits_a_workstation_task_from_a_hive(rig, capsys) -> None:
    assert rig.admitter.admit(SLUG, ask("desk", "hive-a")) is None
    assert rig.store.claimant(SLUG, "desk") is None
    assert rig.store.agents(SLUG) == []
    assert f"hub: events placement swarm={SLUG} task=desk hive=hive-a" in capsys.readouterr().err


def test_the_hub_admits_a_hive_pinned_task_only_from_its_hive(rig) -> None:
    assert rig.admitter.admit(SLUG, ask("pinned", "hive-a")) is None
    assert rig.store.claimant(SLUG, "pinned") is None
    admitted = rig.admitter.admit(SLUG, ask("pinned", "hive-b"))
    assert admitted["hive"] == "hive-b"
    assert rig.store.claimant(SLUG, "pinned") == admitted["name"]


def test_the_hub_admits_a_needs_task_only_from_a_hive_declaring_every_need(rig) -> None:
    registry.update(rig.redis, "hive-a", ["capabilities=headed"])
    assert rig.admitter.admit(SLUG, ask("gpu", "hive-a")) is None
    registry.update(rig.redis, "hive-c", ["capabilities=gpu"])
    assert rig.admitter.admit(SLUG, ask("gpu", "hive-c"))["hive"] == "hive-c"


def test_a_lane_spoke_skips_an_offer_pinned_to_another_hive_without_asking_the_hub(monkeypatch) -> None:
    scoped = open_bus(BusConfig(scoped=True))
    hub = Mock()
    slots.serve(scoped, admitter=hub)
    monkeypatch.setattr(spoke.Spoke, "_measure", lambda self: {"cpus": 32, "load5": 0})
    readiness.publish(scoped.for_swarm(SLUG), snapshot(doc([task("pinned", hive="hive-b")])), NOW)
    configs = SimpleNamespace(
        slugs=lambda: [SLUG], config=lambda slug: SwarmConfig(slug, "/repo", 1, 1, engine="events")
    )

    assert lanes.run_once(configs, scoped, Mock(), "hive-a", ("eng",), 0, {}) == ["empty"]
    hub.admit.assert_not_called()
    scoped.close()


def test_a_workstation_task_launches_from_the_workstation_tick_under_events(store) -> None:  # noqa: F811
    store.update("sw", engine="events")
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FakeRuntime()
    ledger.rows["t2"]["workstation"] = True

    tick("sw", store, ledger, runtime, now_ms=1_000)

    assert [task_id for _, _, task_id in runtime.spawned] == ["t2"]
    assert ledger.rows["t1"]["state"] == "open"
    assert ledger.rows["t2"]["state"] == "claimed"


def test_under_events_the_tick_collects_acknowledgements_only_for_workstation_launches(store) -> None:  # noqa: F811
    from scripts.swarm.tick import _collect_acks
    from tests.swarm.test_tick import HiveAcks

    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), HiveAcks()
    ledger.rows["t2"]["workstation"] = True
    tick("sw", store, ledger, runtime, now_ms=1_000)
    desk = ledger.rows["t2"]["claimed_by"]
    store.update("sw", engine="events")
    doc = {"tasks": list(ledger.rows.values())}

    _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 2_000)

    assert runtime.collected == [desk]


def test_under_events_the_tick_places_only_while_a_workstation_task_is_open() -> None:
    from scripts.swarm.store import EVENTS_ENGINE

    rows = {"a": task("a"), "b": task("b", workstation=True, state="claimed")}
    assert readiness.tick_places("legacy", {})
    assert not readiness.tick_places(EVENTS_ENGINE, rows)
    rows["b"]["state"] = "open"
    assert readiness.tick_places(EVENTS_ENGINE, rows)
    assert readiness.tick_launches("legacy", task("a"))
    assert not readiness.tick_launches(EVENTS_ENGINE, task("a"))
