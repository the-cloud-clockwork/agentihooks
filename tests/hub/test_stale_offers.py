"""Withdrawn ready offers leave the lane stream before the hub publishes fresh ones."""

from pathlib import Path

import pytest

from scripts.events.config import QueuePolicy
from scripts.events.envelope import Envelope
from scripts.hub.readiness import Readiness
from scripts.swarm import readiness
from tests.hub.test_readiness import backend, source
from tests.swarm.test_readiness import doc, task

FEATURE = "hub-withdraws-stale-offers"


def admit_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.swarm.release_wait.checker", lambda _: lambda task: True)
    monkeypatch.setattr("scripts.swarm.apply_wait.checker", lambda *_: lambda task: True)


def offered(key: str, generation: int) -> Envelope:
    return Envelope(
        id=f"ready:rig:{key}:{generation}",
        type="task.ready",
        swarm="rig",
        entity="task",
        key=key,
        version=generation,
        time=1,
        author=readiness.AUTHOR,
        data={"lane": "eng", "generation": generation},
    )


@pytest.mark.feature(FEATURE)
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
        Readiness(bus, provider, clock=lambda: 73).poll()
        delivery = queue.take(1)
        assert delivery is not None
        assert delivery.event == readiness.offer(row, provider.snapshot("rig"), 73)
        delivery.ack()
        assert queue.take(1) is None


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize("kind", ["memory", "nats"])
def test_withdraw_removes_only_the_offers_named_stale_and_counts_them(kind: str, tmp_path: Path) -> None:
    with backend(kind, tmp_path) as bus:
        queue = bus.queue(readiness.queue_name("rig", "eng"), QueuePolicy())
        for key in ["old-1", "keep", "old-2"]:
            assert queue.offer(offered(key, 1))
        assert queue.withdraw(lambda event: event.key.startswith("old")) == 2
        assert queue.withdraw(lambda event: event.key.startswith("old")) == 0
        delivery = queue.take(1)
        assert delivery is not None
        assert delivery.event == offered("keep", 1)
        delivery.ack()
        assert queue.take(1) is None


@pytest.mark.feature(FEATURE)
def test_withdraw_stale_sweeps_every_lane_with_the_withdrawal_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    admit_all(monkeypatch)
    row, store, ledger = source()
    ledger.state.return_value = doc([row, task("ci-done", lane="ci", state="done")])
    view = readiness.Source(store, ledger).snapshot("rig")
    with backend("memory", None) as bus:
        eng = bus.queue(readiness.queue_name("rig", "eng"), QueuePolicy())
        ci = bus.queue(readiness.queue_name("rig", "ci"), QueuePolicy())
        assert eng.offer(offered("task-a", 3))
        assert eng.offer(offered("task-a", 1))
        assert ci.offer(Envelope(**{**vars(offered("ci-done", 0)), "data": {"lane": "ci", "generation": 0}}))
        assert readiness.withdraw_stale(bus, view) == 2
        kept = eng.take(1)
        assert kept is not None
        assert kept.event == offered("task-a", 3)
        assert ci.take(1) is None
