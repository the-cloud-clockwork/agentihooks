from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.events.config import BusConfig
from scripts.hive.spoke import Spoke, SpokeConfig
from scripts.hub import registry
from scripts.hub.runner import HubRunner
from scripts.swarm import readiness
from scripts.swarm_v2.runtime.base import Outcome, SpawnRequest, Status
from tests.hive.test_spoke import reading
from tests.swarm.test_readiness import doc, task


@contextmanager
def backend(kind, tmp_path):
    if kind == "memory":
        from scripts.events.memory import open_bus

        bus = open_bus(BusConfig())
        try:
            yield bus
        finally:
            bus.close()
    else:
        from scripts.events.nats import open_bus
        from tests.events import nats_server

        with nats_server.running(tmp_path / "nats") as url:
            bus = open_bus(BusConfig(backend="nats", url=url))
            try:
                yield bus
            finally:
                bus.close()


def source():
    row = task("task-a", claim_generation=3)
    document = doc([row])
    store, ledger = Mock(), Mock()
    store.slugs.return_value = ["rig"]
    store.config.return_value = SimpleNamespace(slug="rig", state="running", repo="", template="")
    store.agents.return_value = []
    store.claimant.return_value = None
    ledger.state.return_value = document
    return row, store, ledger


def spoke(bus, provider, hive):
    config = SpokeConfig(SimpleNamespace(slug="rig"), "eng", hive)
    tasks, runtime = Mock(), Mock()
    tasks.request.return_value = SpawnRequest(
        config.swarm,
        "eng",
        hive,
        {"id": "task-a", "execution_id": hive, "execution_generation": 1, "claim_generation": 3},
    )
    runtime.spawn.return_value = Outcome(
        "spawn", Status.OK, "local", SimpleNamespace(pane_id="pane", harness="claude", account="slot")
    )
    return Spoke(config, bus, runtime, tasks, reading, view=lambda: provider.snapshot("rig")), runtime


@pytest.mark.parametrize("kind", ["memory", "nats"])
def test_hub_offers_are_taken_once_and_withdrawn_when_frozen_or_claimed(kind, tmp_path, monkeypatch):
    from scripts.hub.readiness import Readiness

    monkeypatch.setattr("scripts.swarm.release_wait.checker", lambda _: lambda task: True)
    monkeypatch.setattr("scripts.swarm.apply_wait.checker", lambda *_: lambda task: True)
    row, store, ledger = source()
    provider = readiness.Source(store, ledger)
    with backend(kind, tmp_path) as bus:
        reactor = Readiness(bus, provider).reactor()
        hub = HubRunner(bus, registry.Registry([reactor]), "hub")
        try:
            first, runtime = spoke(bus, provider, "hive-a")
            second, other_runtime = spoke(bus, provider, "hive-b")
            hub.run_once(timeout_ms=1)
            hub.run_once(timeout_ms=1)
            assert first.poll(100) == "started"
            assert second.poll(10) == "empty"
            runtime.spawn.assert_called_once()
            other_runtime.spawn.assert_not_called()

            row["claim_generation"] += 1
            hub.run_once(timeout_ms=1)
            ledger.state.return_value["freezes"] = [{"verb": "freeze", "target": "tasks/task-a"}]
            assert first.poll(10) == "empty"
            assert runtime.spawn.call_count == 1

            ledger.state.return_value["freezes"] = []
            row["claim_generation"] += 1
            hub.run_once(timeout_ms=1)
            store.claimant.return_value = "other"
            assert second.poll(10) == "empty"
            other_runtime.spawn.assert_not_called()
        finally:
            hub.stop()


def test_configured_hub_has_a_singleton_readiness_reactor():
    with backend("memory", None) as bus:
        reactor = next(r for r in registry.configured(bus) if r.name == "readiness")
        assert reactor.singleton
        assert callable(reactor.poll)


def test_snapshot_uses_live_owners_and_release_and_apply_admission(monkeypatch):
    row, store, ledger = source()
    released, applied = Mock(return_value=True), Mock(return_value=True)
    monkeypatch.setattr("scripts.swarm.release_wait.checker", lambda _: released)
    monkeypatch.setattr("scripts.swarm.apply_wait.checker", lambda *_: applied)
    provider = readiness.Source(store, ledger)
    view = provider.snapshot("rig")
    assert readiness.claimable(row, "eng", view)
    released.return_value = False
    assert not readiness.claimable(row, "eng", view)
    released.return_value = True
    applied.return_value = False
    assert not readiness.claimable(row, "eng", view)
    applied.return_value = True
    store.agents.return_value = [SimpleNamespace(task="task-a", state="idle")]
    assert not readiness.claimable(row, "eng", provider.snapshot("rig"))
    store.agents.return_value = [SimpleNamespace(task="task-a", state="finished")]
    assert readiness.claimable(row, "eng", provider.snapshot("rig"))
    store.config.return_value = SimpleNamespace(**{**vars(store.config.return_value), "state": "paused"})
    assert not readiness.claimable(row, "eng", provider.snapshot("rig"))


def test_hub_respects_its_lease_and_passes_clock_to_ready_offers(monkeypatch):
    from scripts.hub.readiness import Readiness

    monkeypatch.setattr("scripts.swarm.release_wait.checker", lambda _: lambda task: True)
    monkeypatch.setattr("scripts.swarm.apply_wait.checker", lambda *_: lambda task: True)
    row, store, ledger = source()
    provider = readiness.Source(store, ledger)
    with backend("memory", None) as bus:
        reactor = Readiness(bus, provider, clock=lambda: 73)
        reactor.poll(lambda: False)
        store.slugs.assert_not_called()
        reactor.poll()
        queue = bus.queue(readiness.queue_name("rig", "eng"))
        delivery = queue.take(1)
        assert delivery.event == readiness.offer(row, provider.snapshot("rig"), 73)
        delivery.ack()
        reactor.poll()
        assert queue.take(1) is None
