from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.events.config import BusConfig, QueuePolicy
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
    store.config.return_value = SimpleNamespace(slug="rig", state="running", repo="", template="", engine="events")
    store.agents.return_value = []
    store.claimant.return_value = None
    ledger.state.return_value = document
    ledger.metadata.side_effect = lambda _: {"_meta": {"rev": id(ledger.state.return_value)}}
    return row, store, ledger


class CountingLedger:
    def __init__(self, document):
        self.document, self.rev, self.exports, self.read = document, 1, 0, set()

    def state(self, slug):
        self.exports += 1
        return self.document

    def metadata(self, slug):
        self.read.add(slug)
        return {"_meta": {"rev": self.rev}}


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


def test_readiness_passes_export_a_large_ledger_once_per_revision(monkeypatch):
    from scripts.hub.readiness import Readiness

    monkeypatch.setattr("scripts.swarm.release_wait.checker", lambda _: lambda task: True)
    monkeypatch.setattr("scripts.swarm.apply_wait.checker", lambda *_: lambda task: True)
    rows = [task(f"done-{n}", state="done") for n in range(950)] + [task("task-a")]
    ledger = CountingLedger(doc(rows))
    _, store, _ = source()
    provider = readiness.Source(store, ledger)
    with backend("memory", None) as bus:
        hub = Readiness(bus, provider)
        for _ in range(21):
            hub.poll()
        unchanged = ledger.exports
        ledger.document, ledger.rev = doc([*rows, task("task-b")]), 2
        for _ in range(21):
            hub.poll()
        changed = ledger.exports - unchanged
        assert (unchanged, changed) == (1, 1), (
            f"exports over 21 passes: {unchanged} unchanged, {changed} after one revision"
        )
        assert "task-b" in provider.snapshot("rig").rows
        assert ledger.exports == 2
        assert ledger.read == {"rig"}


def test_the_source_forgets_the_export_of_a_swarm_that_left_the_events_engine():
    _, store, _ = source()
    ledger = CountingLedger(doc([task("task-a")]))
    engines = {"rig": "events", "kept": "events"}
    store.slugs.return_value = ["rig", "kept"]
    store.config.side_effect = lambda slug: SimpleNamespace(
        slug=slug, state="running", repo="", template="", engine=engines[slug]
    )
    provider = readiness.Source(store, ledger)
    assert provider.slugs() == ["rig", "kept"]
    provider.state("rig")
    provider.state("kept")
    engines["rig"] = "legacy"
    assert provider.slugs() == ["kept"]
    provider.state("kept")
    assert ledger.exports == 2
    provider.state("rig")
    assert ledger.exports == 3


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
        queue = bus.queue(readiness.queue_name("rig", "eng"), QueuePolicy())
        delivery = queue.take(1)
        assert delivery.event == readiness.offer(row, provider.snapshot("rig"), 73)
        delivery.ack()
        reactor.poll()
        assert queue.take(1) is None


def test_freeze_while_hive_waits_withdraws_the_delivered_offer(monkeypatch):
    from scripts.hub.readiness import Readiness

    monkeypatch.setattr("scripts.swarm.release_wait.checker", lambda _: lambda task: True)
    monkeypatch.setattr("scripts.swarm.apply_wait.checker", lambda *_: lambda task: True)
    _, store, ledger = source()
    provider = readiness.Source(store, ledger)
    with backend("memory", None) as bus:
        Readiness(bus, provider).poll()
        queue = bus.queue(readiness.queue_name("rig", "eng"), QueuePolicy())
        take = queue.take

        def freeze_during_take(timeout_ms):
            delivery = take(timeout_ms)
            ledger.state.return_value = {
                **ledger.state.return_value,
                "freezes": [{"verb": "freeze", "target": "tasks/task-a"}],
            }
            return delivery

        monkeypatch.setattr(queue, "take", freeze_during_take)
        worker, runtime = spoke(bus, provider, "hive")
        assert worker.poll(1) == "empty"
        runtime.spawn.assert_not_called()


def test_default_source_connects_once_and_reads_fresh_shared_state(monkeypatch):
    _, store, ledger = source()
    connect, client = Mock(return_value=store), Mock(return_value=ledger)
    monkeypatch.setattr(readiness, "connect", connect)
    monkeypatch.setattr(readiness, "LedgerClient", client)
    monkeypatch.setattr("scripts.swarm.release_wait.checker", lambda _: lambda task: True)
    monkeypatch.setattr("scripts.swarm.apply_wait.checker", lambda *_: lambda task: True)
    provider = readiness.Source()
    connect.assert_not_called()
    assert provider.slugs() == ["rig"]
    assert provider.snapshot("rig").rows["task-a"]["claim_generation"] == 3
    ledger.state.return_value = doc([task("task-b")])
    assert list(provider.snapshot("rig").rows) == ["task-b"]
    connect.assert_called_once_with()
    client.assert_called_once_with(service=True)
    assert ledger.state.call_args_list == [(("rig",),), (("rig",),)]


def test_the_source_lists_only_swarms_on_the_events_engine():
    _, store, ledger = source()
    store.slugs.return_value = ["one", "two", "three"]
    engines = {"one": "events", "two": "legacy", "three": "events"}
    store.config.side_effect = lambda slug: SimpleNamespace(slug=slug, engine=engines[slug])
    assert readiness.Source(store, ledger).slugs() == ["one", "three"]
    engines["one"] = "legacy"
    assert readiness.Source(store, ledger).slugs() == ["three"]


def test_the_source_skips_a_swarm_removed_while_it_lists():
    from scripts.swarm.store import SwarmError

    _, store, ledger = source()
    store.slugs.return_value = ["gone", "kept"]

    def config(slug):
        if slug == "gone":
            raise SwarmError("no swarm gone")
        return SimpleNamespace(slug=slug, engine="events")

    store.config.side_effect = config
    assert readiness.Source(store, ledger).slugs() == ["kept"]


def test_default_hub_readiness_uses_the_shared_source_and_stops_after_lease_loss(monkeypatch):
    from scripts.hub.readiness import Readiness

    provider = Mock()
    provider.slugs.return_value = ["rig", "other"]
    provider.snapshot.return_value = Mock()
    factory = Mock(return_value=provider)
    publish = Mock()
    monkeypatch.setattr("scripts.hub.readiness.BusConfig.from_environ", lambda: BusConfig(backend="nats"))
    monkeypatch.setattr(readiness, "Source", factory)
    monkeypatch.setattr(readiness, "publish", publish)
    with backend("memory", None) as bus:
        reactor = Readiness(bus, clock=lambda: 73)
        reactor.poll(Mock(side_effect=[True, False]))
        publish.assert_not_called()
        provider.snapshot.assert_called_once_with("rig")
        provider.slugs.return_value = ["rig"]
        reactor.poll()
        factory.assert_called_once_with()
        publish.assert_called_once_with(bus, provider.snapshot.return_value, 73)


def test_unconfigured_memory_hub_does_not_open_shared_services(monkeypatch):
    from scripts.hub.readiness import Readiness

    factory = Mock()
    monkeypatch.setattr(readiness, "Source", factory)
    monkeypatch.setattr("scripts.hub.readiness.BusConfig.from_environ", lambda: BusConfig())
    with backend("memory", None) as bus:
        Readiness(bus).poll()
    factory.assert_not_called()


def test_snapshot_preserves_swarm_scope_and_admission_arguments(monkeypatch):
    row, store, ledger = source()
    released, applied = Mock(return_value=True), Mock(return_value=True)
    release_factory, apply_factory = Mock(return_value=released), Mock(return_value=applied)
    monkeypatch.setattr("scripts.swarm.release_wait.checker", release_factory)
    monkeypatch.setattr("scripts.swarm.apply_wait.checker", apply_factory)
    view = readiness.Source(store, ledger).snapshot("rig")
    store.config.assert_called_once_with("rig")
    store.agents.assert_called_once_with("rig")
    release_factory.assert_called_once_with(store.config.return_value.repo)
    apply_factory.assert_called_once_with(store.config.return_value, {"task-a": row})
    assert view.fix == ""
    assert view.claimed("task-a") is False
    store.claimant.assert_called_once_with("rig", "task-a")
    assert view.admits(row)
    released.assert_called_once_with(row)
    applied.assert_called_once_with(row)


def test_configured_readiness_publishes_through_its_bus_and_clock(monkeypatch):
    from scripts.hub import readiness as hub_readiness

    _, store, ledger = source()
    provider = readiness.Source(store, ledger)
    monkeypatch.setattr(readiness, "Source", lambda: provider)
    monkeypatch.setattr(hub_readiness.BusConfig, "from_environ", lambda: BusConfig(backend="nats"))
    monkeypatch.setattr("scripts.swarm.release_wait.checker", lambda _: lambda task: True)
    monkeypatch.setattr("scripts.swarm.apply_wait.checker", lambda *_: lambda task: True)
    with backend("memory", None) as bus:
        reactor = next(r for r in registry.configured(bus, clock=lambda: 73) if r.name == "readiness")
        assert reactor.subject == "swarm.*.readiness.requested"
        reactor.poll()
        delivery = bus.queue(readiness.queue_name("rig", "eng"), QueuePolicy()).take(1)
        assert delivery.event.time == 73
        delivery.ack()
