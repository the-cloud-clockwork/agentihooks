"""Workstation, hive pinned and needs pinned tasks under the events engine: offers, spoke takes, hub admission, tick."""

from collections.abc import Iterator
from copy import deepcopy
from dataclasses import replace
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
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.helpers import narrow_ledger
from tests.swarm.test_readiness import NOW, SLUG, Clock, bus, clock, doc, snapshot, task  # noqa: F401
from tests.swarm.test_tick import FakeRuntime, store, tasks  # noqa: F401

pytestmark = pytest.mark.unit

OTHER = "sw-b"

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


def focus(target: str, at: int) -> dict:
    return {"id": f"focus-{target}", "verb": "focus", "target": target, "by": "operator", "at": at, "reason": ""}


@pytest.mark.feature("swarm-focus")
def test_focused_offers_go_out_by_rank_band_then_focus_record_order() -> None:
    found = doc(
        [task("old"), task("hot", rank="urgent"), task("new")],
        freezes=[focus("tasks/old", 1), focus("tasks/hot", 2), focus("tasks/new", 3)],
    )
    assert [event.key for event in readiness.offers(snapshot(found), NOW)] == ["hot", "old", "new"]


@pytest.mark.feature("swarm-focus")
def test_a_focused_offer_goes_out_before_an_urgent_doctor_fix_outside_the_focus() -> None:
    found = doc([task("fix", rank="urgent"), task("hot")], freezes=[focus("tasks/hot", 1)])
    assert [event.key for event in readiness.offers(snapshot(found, fix="p1"), NOW)] == ["hot", "fix"]


@pytest.mark.feature("swarm-focus")
def test_offers_without_a_focus_go_out_by_rank_band_keeping_ledger_order_inside_a_band() -> None:
    found = doc([task("low", rank="low"), task("first"), task("urgent", rank="urgent"), task("second")])
    assert [event.key for event in readiness.offers(snapshot(found), NOW)] == ["urgent", "first", "second", "low"]


def test_an_offer_carries_its_hive_pin_and_needs_and_nothing_for_an_unpinned_task() -> None:
    offered = {event.key: event.data for event in readiness.offers(snapshot(doc(PINNED)), NOW)}
    assert offered == {
        "plain": {"lane": "eng", "generation": 0, "quota": {}},
        "desk": {"lane": "eng", "generation": 0, "quota": {}, "hive": ROOT},
        "pinned": {"lane": "eng", "generation": 0, "quota": {}, "hive": "hive-b"},
        "gpu": {"lane": "eng", "generation": 0, "quota": {}, "needs": ["gpu"]},
    }


def test_a_workstation_task_pinned_off_the_root_hive_is_never_offered() -> None:
    pinned = [
        task("desk", workstation=True, hive="hive-b"),
        task("root", workstation=True, hive=ROOT),
        task("blank", workstation=True, hive=""),
    ]
    assert [event.data for event in readiness.offers(snapshot(doc(pinned)), NOW)] == [
        {"lane": "eng", "generation": 0, "quota": {}, "hive": ROOT},
        {"lane": "eng", "generation": 0, "quota": {}, "hive": ROOT},
    ]


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


@pytest.mark.feature("unfit-refusal-spends-no-delivery")
def test_refusals_by_hives_outside_its_pin_spend_none_of_an_offers_deliveries(bus: Bus, clock: Clock) -> None:  # noqa: F811
    readiness.publish(bus, snapshot(doc([task("pinned", hive="hive-b")])), NOW)
    queue = bus.queue(readiness.queue_name(SLUG, "eng"), QueuePolicy())

    for _ in range(QueuePolicy().max_deliver + 1):
        assert (
            readiness.take(queue, lambda: None, 0, lambda event: readiness.fits(event, "hive-a", no_capabilities))
            is None
        )
        clock.now += readiness.UNFIT_NAK_MS

    held = readiness.take(queue, lambda: None, 0, lambda event: readiness.fits(event, "hive-b", no_capabilities))
    assert held is not None
    assert (held.event.key, held.attempt) == ("pinned", 1)


@pytest.mark.feature("unfit-refusal-spends-no-delivery")
def test_a_skipped_offer_is_released_for_another_hive_not_terminated() -> None:
    delivery = SimpleNamespace(event=SimpleNamespace(key="pinned"), nak=Mock(), term=Mock())
    queue = SimpleNamespace(take=Mock(side_effect=[delivery, None]), release=Mock())

    assert readiness.take(cast(WorkQueue, queue), lambda: None, 0, lambda event: False) is None

    queue.release.assert_called_once_with(delivery, readiness.UNFIT_NAK_MS)
    delivery.nak.assert_not_called()
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
        slugs=lambda: [SLUG],
        snapshot=lambda slug: snapshot(found, claimed=[t for t in rows if swarm.claimant(slug, t)]),
    )
    admitter = admission.Admission(swarm, cast(LedgerClient, ledger), cast(readiness.Source, source), lambda: 7_000)
    yield SimpleNamespace(store=swarm, redis=redis, admitter=narrow_ledger.viewed(admitter))
    redis.close()


def ask(task_id: str, hive: str) -> dict:
    return {"hive": hive, "task": task_id, "lane": "eng", "generation": 0}


def test_the_hub_admits_a_workstation_task_only_from_the_root_hive(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rig.admitter.admit(SLUG, ask("desk", "hive-a")) == {
        "held": "the task is workstation off the root hive, pinned to another hive or needs what the hive lacks"
    }
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
    assert rig.admitter.admit(SLUG, ask("pinned", "hive-a")) == {
        "held": "the task is workstation off the root hive, pinned to another hive or needs what the hive lacks"
    }
    assert rig.store.claimant(SLUG, "pinned") is None
    admitted = rig.admitter.admit(SLUG, ask("pinned", "hive-b"))
    assert admitted["hive"] == "hive-b"
    assert rig.store.claimant(SLUG, "pinned") == admitted["name"]


def test_the_hub_admits_a_needs_task_only_from_a_hive_declaring_every_need(rig: SimpleNamespace) -> None:
    registry.update(rig.redis, "hive-a", ["capabilities=headed"])
    assert rig.admitter.admit(SLUG, ask("gpu", "hive-a")) == {
        "held": "the task is workstation off the root hive, pinned to another hive or needs what the hive lacks"
    }
    registry.update(rig.redis, "hive-c", ["capabilities=gpu"])
    assert rig.admitter.admit(SLUG, ask("gpu", "hive-c"))["hive"] == "hive-c"


KEPT = "0 of 1 agents on the hive with 1 kept for tasks only the root hive takes"


@pytest.mark.feature("root-keeps-slots-for-pinned-tasks")
@pytest.mark.parametrize(
    "row, expected",
    [
        (task("plain"), False),
        (task("desk", workstation=True), True),
        (task("blank", workstation=True, hive=""), True),
        (task("root", hive=ROOT), True),
        (task("desk", workstation=True, hive="hive-b"), False),
        (task("pinned", hive="hive-b"), False),
    ],
)
def test_root_only_names_the_tasks_only_the_root_hive_takes(row: dict[str, object], expected: bool) -> None:
    assert readiness.root_only(row) is expected


@pytest.mark.feature("root-keeps-slots-for-pinned-tasks")
def test_root_waiting_counts_only_claimable_tasks_the_root_hive_alone_takes_and_can_place() -> None:
    rows = [
        task("plain"),
        task("desk", workstation=True),
        task("root", hive=ROOT, lane="ci"),
        task("headed", workstation=True, needs=["headed"]),
        task("gpu", workstation=True, needs=["gpu"]),
        task("claimed", workstation=True),
        task("blocked", workstation=True, state="blocked"),
        task("laneless", workstation=True, lane=""),
        task("elsewhere", workstation=True, hive="hive-b"),
    ]
    assert readiness.root_waiting(snapshot(doc(rows), claimed=["claimed"]), lambda: ["headed"]) == 3


def _capped(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caps: dict[str, int]) -> None:
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")
    for hive, cap in caps.items():
        registry.beat(rig.redis, hive, {"heartbeat_at": 6_000})
        registry.update(rig.redis, hive, [f"max-agents={cap}"])


@pytest.mark.feature("root-keeps-slots-for-pinned-tasks")
def test_root_with_one_free_slot_holds_the_unpinned_task_for_another_hive_and_admits_the_workstation_task(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _capped(rig, monkeypatch, {ROOT: 1, "hive-a": 1})

    assert rig.admitter.admit(SLUG, ask("plain", ROOT)) == {"held": KEPT}
    assert rig.store.claimant(SLUG, "plain") is None
    elsewhere = rig.admitter.admit(SLUG, ask("plain", "hive-a"))
    assert elsewhere["hive"] == "hive-a"
    admitted = rig.admitter.admit(SLUG, ask("desk", ROOT))
    assert admitted["hive"] == ROOT
    assert rig.store.claimant(SLUG, "desk") == admitted["name"]
    assert capsys.readouterr().err.splitlines() == [
        f"hub: events admission swarm={SLUG} task=plain hive={ROOT} refused: {KEPT}"
    ]


@pytest.mark.feature("root-keeps-slots-for-pinned-tasks")
@pytest.mark.parametrize("capabilities, held", [((), False), (("gpu",), True)])
def test_root_keeps_a_slot_only_for_a_workstation_task_it_can_place(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capabilities: tuple[str, ...], held: bool
) -> None:
    _capped(rig, monkeypatch, {ROOT: 1})
    if capabilities:
        registry.update(rig.redis, ROOT, [f"capabilities={','.join(capabilities)}"])
    found = doc([task("plain"), task("desk", workstation=True, needs=["gpu"])])
    monkeypatch.setattr(
        rig.admitter, "source", SimpleNamespace(slugs=lambda: [SLUG], snapshot=lambda slug: snapshot(found))
    )

    admitted = rig.admitter.admit(SLUG, ask("plain", ROOT))

    assert admitted is not None
    assert ("held" in admitted) is held
    assert (rig.store.claimant(SLUG, "plain") is None) is held


@pytest.mark.feature("root-keeps-slots-for-pinned-tasks")
def test_root_admits_unpinned_work_only_into_slots_beyond_the_waiting_workstation_tasks(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capped(rig, monkeypatch, {ROOT: 2})
    registry.update(rig.redis, ROOT, ["capabilities=gpu"])

    admitted = rig.admitter.admit(SLUG, ask("plain", ROOT))

    assert admitted["hive"] == ROOT
    assert rig.store.claimant(SLUG, "plain") == admitted["name"]
    assert rig.admitter.admit(SLUG, ask("gpu", ROOT)) == {
        "held": "1 of 2 agents on the hive with 1 kept for tasks only the root hive takes"
    }


@pytest.mark.feature("root-keeps-slots-for-pinned-tasks")
def test_root_keeps_a_slot_for_a_workstation_task_another_events_swarm_holds_waiting(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _capped(rig, monkeypatch, {ROOT: 1})
    rig.store.create(SwarmConfig(OTHER, "/repo", 2, 2))
    claimed: list[str] = []
    views = {
        SLUG: snapshot(doc([task("plain")])),
        OTHER: replace(snapshot(doc([task("desk", workstation=True)]), claimed=claimed), slug=OTHER),
    }
    monkeypatch.setattr(
        rig.admitter, "source", SimpleNamespace(slugs=lambda: [SLUG, OTHER], snapshot=views.__getitem__)
    )

    assert rig.admitter.admit(SLUG, ask("plain", ROOT)) == {"held": KEPT}
    assert rig.store.claimant(SLUG, "plain") is None
    claimed.append("desk")
    admitted = rig.admitter.admit(SLUG, ask("plain", ROOT))
    assert admitted["hive"] == ROOT
    assert rig.store.claimant(SLUG, "plain") == admitted["name"]
    assert capsys.readouterr().err.splitlines() == [
        f"hub: events admission swarm={SLUG} task=plain hive={ROOT} refused: {KEPT}"
    ]


@pytest.mark.feature("root-keeps-slots-for-pinned-tasks")
def test_root_keeps_one_slot_for_each_waiting_workstation_task_of_every_events_swarm(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capped(rig, monkeypatch, {ROOT: 2})
    rig.store.create(SwarmConfig(OTHER, "/repo", 2, 2))
    views = {
        SLUG: snapshot(doc([task("plain"), task("desk", workstation=True)])),
        OTHER: replace(snapshot(doc([task("desk", workstation=True)])), slug=OTHER),
    }
    monkeypatch.setattr(
        rig.admitter, "source", SimpleNamespace(slugs=lambda: [OTHER, SLUG], snapshot=views.__getitem__)
    )

    assert rig.admitter.admit(SLUG, ask("plain", ROOT)) == {
        "held": "0 of 2 agents on the hive with 2 kept for tasks only the root hive takes"
    }
    assert rig.store.claimant(SLUG, "plain") is None


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


@pytest.mark.feature("workstation-events-route")
def test_a_workstation_task_under_events_is_offered_admitted_and_launched_on_the_root_hive(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import serena_router_daemon

    scoped = open_bus(BusConfig(scoped=True))
    slots.serve(cast(Bus, scoped), admitter=rig.admitter)
    monkeypatch.setattr(spoke.Spoke, "_measure", lambda self: {"cpus": 32, "load5": 0})
    monkeypatch.setattr(serena_router_daemon, "ensure", lambda: True)
    found = doc([task("desk", workstation=True)])
    assert readiness.publish(scoped.for_swarm(SLUG), snapshot(found), NOW) == [f"ready:{SLUG}:desk:0:{ROOT}"]
    configs = SimpleNamespace(
        slugs=lambda: [SLUG], config=lambda slug: SwarmConfig(slug, "/repo", 1, 1, engine="events")
    )
    runtime = _launching()

    assert lanes.run_once(configs, scoped, runtime, ROOT, ("eng",), 0, {}) == ["started"]

    [request] = [call.args[0] for call in runtime.spawn.call_args_list]
    assert request.task["id"] == "desk"
    assert rig.store.claimant(SLUG, "desk") == request.name
    scoped.close()


def _launching() -> Mock:
    from scripts.swarm_v2.runtime.base import Outcome, Status

    runtime = Mock()
    runtime.spawn.return_value = Outcome(
        "spawn",
        Status.OK,
        "local",
        SimpleNamespace(
            pane_id="pane-1",
            harness="claude",
            account="slot-a",
            profile="engineer",
            model="opus",
            effort="high",
            launched_at=0,
        ),
    )
    return runtime


@pytest.mark.feature("root-keeps-slots-for-pinned-tasks")
def test_root_with_one_free_slot_launches_the_workstation_task_on_the_poll_after_holding_the_unpinned_one(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import serena_router_daemon

    _capped(rig, monkeypatch, {ROOT: 1})
    scoped = open_bus(BusConfig(scoped=True))
    slots.serve(cast(Bus, scoped), admitter=rig.admitter)
    monkeypatch.setattr(spoke.Spoke, "_measure", lambda self: {"cpus": 32, "load5": 0})
    monkeypatch.setattr(serena_router_daemon, "ensure", lambda: True)
    found = doc([task("plain"), task("desk", workstation=True)])
    readiness.publish(scoped.for_swarm(SLUG), snapshot(found), NOW)
    configs = SimpleNamespace(
        slugs=lambda: [SLUG], config=lambda slug: SwarmConfig(slug, "/repo", 1, 1, engine="events")
    )
    runtime = _launching()

    assert lanes.run_once(configs, scoped, runtime, ROOT, ("eng",), 0, {}) == ["held"]
    assert lanes.run_once(configs, scoped, runtime, ROOT, ("eng",), 0, {}) == ["started"]

    [request] = [call.args[0] for call in runtime.spawn.call_args_list]
    assert request.task["id"] == "desk"
    assert rig.store.claimant(SLUG, "desk") == request.name
    assert rig.store.claimant(SLUG, "plain") is None
    scoped.close()


@pytest.mark.feature("dead-offer-generation")
def test_a_task_whose_offer_dead_lettered_at_its_current_generation_is_placed_on_a_later_pass(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import serena_router_daemon
    from scripts.swarm_v2.runtime.base import Outcome, Status

    scoped = open_bus(BusConfig(scoped=True))
    slots.serve(cast(Bus, scoped), admitter=rig.admitter)
    monkeypatch.setattr(spoke.Spoke, "_measure", lambda self: {"cpus": 32, "load5": 0})
    monkeypatch.setattr(serena_router_daemon, "ensure", lambda: True)
    configs = SimpleNamespace(
        slugs=lambda: [SLUG],
        config=lambda slug: SwarmConfig(slug, "/repo", 1, 1, engine="events"),
        redis=rig.redis,
    )
    runtime = Mock()
    runtime.spawn.return_value = Outcome(
        "spawn",
        Status.OK,
        "local",
        SimpleNamespace(
            pane_id="pane-1",
            harness="claude",
            account="slot-a",
            profile="engineer",
            model="opus",
            effort="high",
            launched_at=0,
        ),
    )
    [offered] = [event for event in readiness.offers(rig.admitter.source.snapshot(SLUG), NOW) if event.key == "plain"]
    scoped.for_swarm(SLUG).state("spoke-work").create(spoke.claim_key(offered), b'{"state": "dead"}')
    assert f"ready:{SLUG}:plain:0" in readiness.publish(scoped.for_swarm(SLUG), rig.admitter.source.snapshot(SLUG), NOW)

    assert lanes.run_once(configs, scoped, runtime, "hive-a", ("eng",), 0, {}) == ["dead"]
    assert readiness.publish(scoped.for_swarm(SLUG), rig.admitter.source.snapshot(SLUG), NOW) == [
        f"ready:{SLUG}:plain:1"
    ]
    assert lanes.run_once(configs, scoped, runtime, "hive-a", ("eng",), 0, {}) == ["started"]

    [request] = [call.args[0] for call in runtime.spawn.call_args_list]
    assert request.task["id"] == "plain"
    assert rig.store.claimant(SLUG, "plain") == request.name
    scoped.close()


@pytest.mark.feature("withdrawn-offer-asked-again")
def test_a_workstation_task_whose_claim_was_withdrawn_at_its_current_generation_is_placed_on_the_root_hive(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import serena_router_daemon
    from scripts.swarm_v2.runtime.base import Outcome, Status

    scoped = open_bus(BusConfig(scoped=True))
    slots.serve(cast(Bus, scoped), admitter=rig.admitter)
    monkeypatch.setattr(spoke.Spoke, "_measure", lambda self: {"cpus": 32, "load5": 0})
    monkeypatch.setattr(serena_router_daemon, "ensure", lambda: True)
    configs = SimpleNamespace(
        slugs=lambda: [SLUG],
        config=lambda slug: SwarmConfig(slug, "/repo", 1, 1, engine="events"),
        redis=rig.redis,
    )
    runtime = Mock()
    runtime.spawn.return_value = Outcome(
        "spawn",
        Status.OK,
        "local",
        SimpleNamespace(
            pane_id="pane-1",
            harness="claude",
            account="slot-a",
            profile="engineer",
            model="opus",
            effort="high",
            launched_at=0,
        ),
    )
    found = doc([task("desk", workstation=True)])
    [offered] = readiness.offers(snapshot(found), NOW)
    scoped.for_swarm(SLUG).state("spoke-work").create(spoke.claim_key(offered), b'{"state": "withdrawn"}')
    assert readiness.publish(scoped.for_swarm(SLUG), snapshot(found), NOW) == [f"ready:{SLUG}:desk:0:{ROOT}"]

    assert lanes.run_once(configs, scoped, runtime, ROOT, ("eng",), 0, {}) == ["started"]

    [request] = [call.args[0] for call in runtime.spawn.call_args_list]
    assert request.task["id"] == "desk"
    assert rig.store.claimant(SLUG, "desk") == request.name
    scoped.close()
