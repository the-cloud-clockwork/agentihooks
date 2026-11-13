"""Workstation, hive pinned and needs pinned tasks under the events engine: offers, spoke takes, hub admission, tick."""

from collections.abc import Iterator
from copy import deepcopy
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from scripts.events.bus import Bus, WorkQueue
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.memory import open_bus
from scripts.hive import lanes, registry, spoke
from scripts.hub import admission, slots
from scripts.swarm import readiness
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.probe import ROOT
from scripts.swarm.store import EVENTS_ENGINE, RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_readiness import NOW, SLUG, Clock, bus, clock, doc, snapshot, task  # noqa: F401
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


def test_a_workstation_task_is_offered_to_its_lane_queue_pinned_to_the_root_hive() -> None:
    offered = {event.key: event for event in readiness.offers(snapshot(doc(PINNED)), NOW)}
    assert list(offered) == ["plain", "desk", "pinned", "gpu"]
    assert offered["desk"].id == f"ready:{SLUG}:desk:0:{ROOT}"
    assert offered["plain"].id == f"ready:{SLUG}:plain:0"


def test_an_offer_carries_its_hive_pin_and_needs_and_nothing_for_an_unpinned_task() -> None:
    offered = {event.key: event.data for event in readiness.offers(snapshot(doc(PINNED)), NOW)}
    assert offered == {
        "plain": {"lane": "eng", "generation": 0},
        "desk": {"lane": "eng", "generation": 0, "hive": ROOT},
        "pinned": {"lane": "eng", "generation": 0, "hive": "hive-b"},
        "gpu": {"lane": "eng", "generation": 0, "needs": ["gpu"]},
    }


@pytest.mark.parametrize(
    "row, hive, capabilities, expected",
    [
        (task("plain"), "hive-a", (), True),
        (task("desk", workstation=True), "hive-a", (), False),
        (task("desk", workstation=True), ROOT, (), True),
        (task("desk", workstation=True, hive="hive-b"), ROOT, (), False),
        (task("pinned", hive="hive-b"), "hive-a", (), False),
        (task("pinned", hive="hive-b"), "hive-b", (), True),
        (task("gpu", needs=["gpu"]), "hive-a", ("headed",), False),
        (task("gpu", needs=["gpu", "headed"]), "hive-a", ("headed", "gpu"), True),
    ],
)
def test_placeable_keeps_workstation_pinned_and_needs_tasks_off_hives_that_may_not_take_them(
    row: dict, hive: str, capabilities: tuple[str, ...], expected: bool
) -> None:
    assert readiness.placeable(row, hive, lambda: capabilities) is expected


def test_fits_reads_capabilities_only_for_an_offer_with_needs() -> None:
    offered = {event.key: event for event in readiness.offers(snapshot(doc(PINNED)), NOW)}
    assert readiness.fits(offered["plain"], "hive-a", no_capabilities)
    assert not readiness.fits(offered["pinned"], "hive-a", no_capabilities)
    assert readiness.fits(offered["pinned"], "hive-b", no_capabilities)
    assert not readiness.fits(offered["gpu"], "hive-a", lambda: ["headed"])
    assert readiness.fits(offered["gpu"], "hive-a", lambda: ["gpu"])


def test_an_empty_hive_pin_accepts_any_hive() -> None:
    assert readiness.placeable({"hive": ""}, "hive-a", no_capabilities)


def test_declared_capabilities_default_to_empty_when_registration_omits_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.hive import registry

    backing = object()
    registered = Mock(return_value={"hive": "hive-a"})
    monkeypatch.setattr(registry, "show", registered)

    assert readiness.declared(SimpleNamespace(redis=backing), "hive-a")() == []
    registered.assert_called_once_with(backing, "hive-a")


def test_lane_fit_preserves_hive_and_registered_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.hive import registry

    backing = object()
    registered = Mock(return_value={"capabilities": ["gpu"]})
    monkeypatch.setattr(registry, "show", registered)
    configured = lanes.fit(SimpleNamespace(redis=backing), "hive-a")
    offered = {event.key: event for event in readiness.offers(snapshot(doc(PINNED)), NOW)}

    assert configured(offered["plain"])
    assert not configured(offered["pinned"])
    home = readiness.offers(snapshot(doc([task("home", hive="hive-a")])), NOW)[0]
    assert configured(home)
    assert configured(offered["gpu"])
    registered.assert_called_once_with(backing, "hive-a")


def test_a_hive_leaves_an_offer_pinned_elsewhere_in_the_queue_for_its_own_hive(bus: Bus, clock: Clock) -> None:  # noqa: F811
    readiness.publish(bus, snapshot(doc([task("pinned", hive="hive-b"), task("plain")])), NOW)
    queue = bus.queue(readiness.queue_name(SLUG, "eng"), QueuePolicy())

    taken = readiness.take(queue, lambda: None, 0, lambda event: readiness.fits(event, "hive-a", no_capabilities))
    assert taken is not None
    assert taken.event.key == "plain"
    taken.ack()
    assert (
        readiness.take(queue, lambda: None, 0, lambda event: readiness.fits(event, "hive-a", no_capabilities)) is None
    )

    clock.now += readiness.UNFIT_NAK_MS
    held = readiness.take(queue, lambda: None, 0, lambda event: readiness.fits(event, "hive-b", no_capabilities))
    assert held is not None
    assert held.event.key == "pinned"


def test_a_skipped_offer_is_released_for_another_hive_not_terminated() -> None:
    delivery = SimpleNamespace(event=SimpleNamespace(key="pinned"), nak=Mock(), term=Mock())
    queue = SimpleNamespace(take=Mock(side_effect=[delivery, None]))

    assert readiness.take(cast(WorkQueue, queue), lambda: None, 0, lambda event: False) is None

    delivery.nak.assert_called_once_with(readiness.UNFIT_NAK_MS)
    delivery.term.assert_not_called()


@pytest.fixture
def rig() -> Iterator[SimpleNamespace]:
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    swarm = RedisStore(redis)
    swarm.create(SwarmConfig(SLUG, "/repo", 2, 2))
    found = doc(deepcopy(PINNED))
    rows = {row["id"]: row for row in found["tasks"]}

    def update_task(slug: str, task_id: str, fields: dict, if_state: tuple = (), if_match: int | None = None) -> dict:
        row = rows[task_id]
        if row["state"] in if_state:
            row["claim_generation"] = row.get("claim_generation", 0) + 1
            row.update(fields)
        return dict(row)

    ledger = SimpleNamespace(update_task=update_task)
    source = SimpleNamespace(
        snapshot=lambda slug: snapshot(found, claimed=[t for t in rows if swarm.claimant(slug, t)])
    )
    yield SimpleNamespace(
        store=swarm,
        redis=redis,
        admitter=admission.Admission(swarm, cast(LedgerClient, ledger), cast(readiness.Source, source), lambda: 7_000),
    )
    redis.close()


def ask(task_id: str, hive: str) -> dict:
    return {"hive": hive, "task": task_id, "lane": "eng", "generation": 0}


def test_the_hub_admits_a_workstation_task_only_from_the_root_hive(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rig.admitter.admit(SLUG, ask("desk", "hive-a")) is None
    assert rig.store.claimant(SLUG, "desk") is None
    assert rig.store.agents(SLUG) == []
    assert capsys.readouterr().err.splitlines() == [
        f"hub: events placement swarm={SLUG} task=desk hive=hive-a: refused, "
        "the task is workstation off the root hive, pinned to another hive or needs what the hive lacks"
    ]
    admitted = rig.admitter.admit(SLUG, ask("desk", ROOT))
    assert admitted["hive"] == ROOT
    assert rig.store.claimant(SLUG, "desk") == admitted["name"]


def test_the_hub_admits_a_hive_pinned_task_only_from_its_hive(rig: SimpleNamespace) -> None:
    assert rig.admitter.admit(SLUG, ask("pinned", "hive-a")) is None
    assert rig.store.claimant(SLUG, "pinned") is None
    admitted = rig.admitter.admit(SLUG, ask("pinned", "hive-b"))
    assert admitted["hive"] == "hive-b"
    assert rig.store.claimant(SLUG, "pinned") == admitted["name"]


def test_the_hub_admits_a_needs_task_only_from_a_hive_declaring_every_need(rig: SimpleNamespace) -> None:
    registry.update(rig.redis, "hive-a", ["capabilities=headed"])
    assert rig.admitter.admit(SLUG, ask("gpu", "hive-a")) is None
    registry.update(rig.redis, "hive-c", ["capabilities=gpu"])
    assert rig.admitter.admit(SLUG, ask("gpu", "hive-c"))["hive"] == "hive-c"


def test_hub_admission_keeps_shared_placement_rows_open(rig: SimpleNamespace) -> None:
    registry.update(rig.redis, "hive-c", ["capabilities=gpu"])
    assert rig.admitter.admit(SLUG, ask("pinned", "hive-b")) is not None
    assert rig.admitter.admit(SLUG, ask("gpu", "hive-c")) is not None

    offered = readiness.offers(snapshot(doc(PINNED)), NOW)
    assert [event.key for event in offered] == ["plain", "desk", "pinned", "gpu"]


def test_a_lane_spoke_skips_an_offer_pinned_to_another_hive_without_asking_the_hub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scoped = open_bus(BusConfig(scoped=True))
    hub = Mock()
    slots.serve(cast(Bus, scoped), admitter=hub)
    monkeypatch.setattr(spoke.Spoke, "_measure", lambda self: {"cpus": 32, "load5": 0})
    readiness.publish(scoped.for_swarm(SLUG), snapshot(doc([task("pinned", hive="hive-b")])), NOW)
    configs = SimpleNamespace(
        slugs=lambda: [SLUG], config=lambda slug: SwarmConfig(slug, "/repo", 1, 1, engine="events")
    )

    assert lanes.run_once(configs, scoped, Mock(), "hive-a", ("eng",), 0, {}) == ["empty"]
    hub.admit.assert_not_called()
    scoped.close()


def test_lane_spokes_bind_fit_to_their_store_and_hive(monkeypatch: pytest.MonkeyPatch) -> None:
    configs = SimpleNamespace(
        slugs=lambda: [SLUG], config=lambda slug: SwarmConfig(slug, "/repo", 1, 1, engine="events")
    )
    expected = Mock()
    fit = Mock(return_value=expected)
    monkeypatch.setattr(lanes, "fit", fit)
    scoped, runtime = Mock(), Mock()

    current = lanes.spokes(configs, scoped, runtime, "hive-a", ("eng",), {})

    fit.assert_called_once_with(configs, "hive-a")
    assert current[0].config.fit is expected


@pytest.mark.feature("workstation-events-route")
def test_under_events_the_tick_launches_no_workstation_task(store: RedisStore) -> None:  # noqa: F811
    store.update("sw", engine="events")
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FakeRuntime()
    ledger.rows["t2"]["workstation"] = True

    tick("sw", store, ledger, runtime, now_ms=1_000)

    assert runtime.spawned == []
    assert ledger.rows["t1"]["state"] == "open"
    assert ledger.rows["t2"]["state"] == "open"


def test_events_tick_does_not_run_placement_even_with_an_open_workstation_task(
    store: RedisStore,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.swarm import tick as tick_module

    store.update("sw", engine="events")
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FakeRuntime()
    ledger.rows["t2"]["workstation"] = True
    spawn = Mock()
    monkeypatch.setattr(tick_module, "_spawn", spawn)

    tick("sw", store, ledger, runtime, now_ms=1_000)

    spawn.assert_not_called()
    assert ledger.rows["t1"]["state"] == "open"


def test_under_events_the_tick_collects_no_acknowledgement_for_a_workstation_launch(store: RedisStore) -> None:  # noqa: F811
    from scripts.swarm.tick import _collect_acks
    from tests.swarm.test_tick import HiveAcks

    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), HiveAcks()
    ledger.rows["t2"]["workstation"] = True
    tick("sw", store, ledger, runtime, now_ms=1_000)
    desk = ledger.rows["t2"]["claimed_by"]
    store.update("sw", engine="events")
    doc = {"tasks": list(ledger.rows.values())}

    _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 2_000)

    assert runtime.collected == []
    assert desk


def test_only_engines_other_than_events_have_the_tick_launch() -> None:
    assert readiness.tick_launches("legacy")
    assert not readiness.tick_launches(EVENTS_ENGINE)


@pytest.mark.feature("workstation-events-route")
def test_a_workstation_task_under_events_is_offered_admitted_and_launched_on_the_root_hive(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm_v2.runtime.base import Outcome, Status

    scoped = open_bus(BusConfig(scoped=True))
    slots.serve(cast(Bus, scoped), admitter=rig.admitter)
    monkeypatch.setattr(spoke.Spoke, "_measure", lambda self: {"cpus": 32, "load5": 0})
    found = doc([task("desk", workstation=True)])
    assert readiness.publish(scoped.for_swarm(SLUG), snapshot(found), NOW) == [f"ready:{SLUG}:desk:0:{ROOT}"]
    configs = SimpleNamespace(
        slugs=lambda: [SLUG], config=lambda slug: SwarmConfig(slug, "/repo", 1, 1, engine="events")
    )
    runtime = Mock()
    runtime.spawn.return_value = Outcome(
        "spawn", Status.OK, "local", SimpleNamespace(pane_id="pane-1", harness="claude", account="slot-a")
    )

    assert lanes.run_once(configs, scoped, runtime, ROOT, ("eng",), 0, {}) == ["started"]

    [request] = [call.args[0] for call in runtime.spawn.call_args_list]
    assert request.task["id"] == "desk"
    assert rig.store.claimant(SLUG, "desk") == request.name
    assert not readiness.tick_launches(EVENTS_ENGINE)
    scoped.close()
