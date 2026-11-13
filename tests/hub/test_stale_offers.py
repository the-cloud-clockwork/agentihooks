"""Withdrawn ready offers leave the lane stream before the hub publishes fresh ones."""

from pathlib import Path
from typing import cast

import pytest

from scripts.events import memory
from scripts.events.bus import Bus
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope
from scripts.events.scope import ScopedQueue
from scripts.hub.readiness import Readiness
from scripts.swarm import readiness
from tests.hub.test_readiness import backend, source
from tests.swarm.test_readiness import doc, task


def admit_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.swarm.release_wait.checker", lambda _: lambda task: True)
    monkeypatch.setattr("scripts.swarm.apply_wait.checker", lambda *_: lambda task: True)


def offered(key: str, generation: int, lane: str = "eng") -> Envelope:
    return Envelope(
        id=f"ready:rig:{key}:{generation}",
        type="task.ready",
        swarm="rig",
        entity="task",
        key=key,
        version=generation,
        time=1,
        author=readiness.AUTHOR,
        data={"lane": lane, "generation": generation},
    )


@pytest.mark.feature("hub-withdraws-stale-offers")
@pytest.mark.parametrize("kind", ["memory", "nats"])
def test_a_lane_stream_with_withdrawn_offers_ahead_delivers_the_fresh_offer_first(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admit_all(monkeypatch)
    row, store, ledger = source()
    gone = [task(f"done-{n}", state="done") for n in range(5)]
    ledger.state.return_value = doc([row, *gone])
    provider = readiness.Source(store, ledger)
    with backend(kind, tmp_path) as bus:
        queue = bus.queue(readiness.queue_name("rig", "eng"), QueuePolicy())
        for event in [*(offered(t["id"], 0) for t in gone), offered("task-a", 2), offered("missing", 0)]:
            assert queue.offer(event)
        Readiness(cast(Bus, bus), provider, clock=lambda: 73).poll()
        delivery = queue.take(1)
        assert delivery is not None
        assert delivery.event == readiness.offer(row, provider.snapshot("rig"), 73)
        delivery.ack()
        assert queue.take(1) is None


@pytest.mark.feature("hub-withdraws-stale-offers")
@pytest.mark.parametrize("kind", ["memory", "nats"])
def test_withdraw_removes_only_the_offers_named_stale_and_counts_them(kind: str, tmp_path: Path) -> None:
    with backend(kind, tmp_path) as bus:
        queue = bus.queue(readiness.queue_name("rig", "eng"), QueuePolicy())
        assert queue.withdraw(lambda event: True) == 0
        for key in ["old-1", "keep", "old-2"]:
            assert queue.offer(offered(key, 1))
        assert queue.withdraw(lambda event: event.key.startswith("old")) == 2
        assert queue.offer(offered("old-3", 1))
        assert queue.withdraw(lambda event: event.key.startswith("old")) == 1
        assert queue.withdraw(lambda event: event.key.startswith("old")) == 0
        delivery = queue.take(1)
        assert delivery is not None
        assert delivery.event == offered("keep", 1)
        delivery.ack()
        assert queue.take(1) is None


@pytest.mark.feature("hub-withdraws-stale-offers")
def test_a_nats_sweep_leaves_an_offer_published_while_it_runs_for_the_next_sweep(tmp_path: Path) -> None:
    with backend("nats", tmp_path) as bus:
        queue = bus.queue(readiness.queue_name("rig", "eng"), QueuePolicy())
        assert queue.offer(offered("old-1", 1))
        late = [offered("old-late", 1)]

        def stale(event: Envelope) -> bool:
            for event_late in late:
                assert queue.offer(event_late)
            late.clear()
            return True

        assert queue.withdraw(stale) == 1
        assert queue.withdraw(stale) == 1
        assert queue.take(1) is None


@pytest.mark.feature("hub-withdraws-stale-offers")
def test_a_swarm_scoped_queue_withdraws_through_its_lane_queue() -> None:
    bus = memory.open_bus(BusConfig(scoped=True))
    try:
        queue = bus.for_swarm("rig").queue(readiness.queue_name("rig", "eng"), QueuePolicy())
        assert isinstance(queue, ScopedQueue)
        assert queue.offer(offered("old-1", 1))
        assert queue.offer(offered("keep", 1))
        assert queue.withdraw(lambda event: event.key == "old-1") == 1
        kept = queue.take(1)
        assert kept is not None
        assert kept.event == offered("keep", 1)
    finally:
        bus.close()


@pytest.mark.feature("hub-withdraws-stale-offers")
def test_publish_withdraws_the_stale_offers_of_every_lane_and_keeps_a_waiting_fresh_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admit_all(monkeypatch)
    row, store, ledger = source()
    ledger.state.return_value = doc([row, task("ci-done", lane="ci", state="done")])
    view = readiness.Source(store, ledger).snapshot("rig")
    with backend("memory", None) as bus:
        eng = bus.queue(readiness.queue_name("rig", "eng"), QueuePolicy())
        ci = bus.queue(readiness.queue_name("rig", "ci"), QueuePolicy())
        assert eng.offer(offered("task-a", 3))
        assert eng.offer(offered("task-a", 1))
        assert ci.offer(offered("ci-done", 0, "ci"))
        assert readiness.publish(cast(Bus, bus), view, 73) == []
        kept = eng.take(1)
        assert kept is not None
        assert kept.event == offered("task-a", 3)
        kept.ack()
        assert eng.take(1) is None
        assert ci.take(1) is None


@pytest.mark.feature("hub-withdraws-stale-offers")
def test_an_offer_whose_task_turned_workstation_is_withdrawn_and_offered_again_to_the_root_hive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admit_all(monkeypatch)
    row, store, ledger = source()
    view = readiness.Source(store, ledger).snapshot("rig")
    assert not readiness.stale(offered("task-a", 3), view)
    assert readiness.stale(offered("task-a", 2), view)
    row["workstation"] = True
    assert readiness.stale(offered("task-a", 3), view)
    with backend("memory", None) as bus:
        eng = bus.queue(readiness.queue_name("rig", "eng"), QueuePolicy())
        assert eng.offer(offered("task-a", 3))
        assert readiness.publish(cast(Bus, bus), view, 73) == ["ready:rig:task-a:3:root"]
        delivery = eng.take(1)
        assert delivery is not None
        assert delivery.event.data["hive"] == "root"
        delivery.ack()
        assert eng.take(1) is None


@pytest.mark.feature("hub-withdraws-stale-offers")
def test_the_controller_grant_lets_the_hub_delete_a_stale_offer_from_any_lane_stream() -> None:
    from scripts.hive import nats_grant

    assert "$JS.API.STREAM.MSG.DELETE.*" in nats_grant.CONTROLLER_PUBLISH
    assert not any(subject.startswith("$JS.API.STREAM.MSG.DELETE") for subject in nats_grant.MEMBER_PUBLISH)
