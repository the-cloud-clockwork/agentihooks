import json
from types import SimpleNamespace

import pytest

from scripts.events import memory
from scripts.events.config import BusConfig, QueuePolicy
from scripts.hive.spoke import claim_key
from scripts.hub import offers, registry
from scripts.hub.registry import Registry
from scripts.hub.runner import HubRunner
from scripts.swarm import readiness
from scripts.swarm_ledger.repository import hierarchy

pytestmark = pytest.mark.unit

SLUG = "sw"
NOW = 1_000_000


def task(task_id, **fields):
    return {"id": task_id, "title": f"title {task_id}", "phase": "p1", "state": "open", "lane": "eng", **fields}


def doc(tasks, **extra):
    return {
        "overview": "o",
        "tasks": list(tasks),
        "phases": [{"id": "p1", "title": "build", "depends_on": []}],
        **extra,
    }


def snapshot(found, claimed=(), slug=SLUG):
    rows = {t["id"]: t for t in found["tasks"]}
    graph = hierarchy.project(found)[0]
    return readiness.Snapshot(slug, rows, found, graph, "", lambda task_id: task_id in claimed, lambda t: True)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class Rig:
    def __init__(self):
        self.clock = Clock()
        self.bus = memory.open_bus(BusConfig(), clock=self.clock)
        self.views = {SLUG: snapshot(doc([]))}
        self.store = SimpleNamespace(slugs=lambda: sorted(self.views))
        self.hub = offers.Readiness(self.bus, self.store, self.views.get, self.clock)

    def later(self):
        self.clock.now += offers.INTERVAL_MS

    def claim(self, event):
        entry = self.bus.state(offers.CLAIMS).get(claim_key(event))
        return json.loads(entry.value) if entry else None

    def queued(self, lane="eng", slug=SLUG):
        queue = self.bus.queue(readiness.queue_name(slug, lane), QueuePolicy())
        taken = []
        while (delivery := queue.take(0)) is not None:
            delivery.ack()
            taken.append(delivery.event.id)
        return taken


@pytest.fixture
def rig():
    return Rig()


def offered(view, task_id):
    return readiness.offer(view.rows[task_id], view, NOW)


def test_a_ready_task_is_offered_within_one_pass_with_only_the_hub_running(rig):
    rig.views[SLUG] = snapshot(doc([task("a"), task("b", lane="ci")]))
    hub = HubRunner(rig.bus, Registry([rig.hub.reactor()]), "hub", rig.clock)
    assert hub.run_once(timeout_ms=0) == {"readiness": []}
    assert rig.queued("eng") == ["ready:sw:a:0"]
    assert rig.queued("ci") == ["ready:sw:b:0"]


def test_a_task_frozen_after_its_offer_is_withdrawn(rig):
    rig.views[SLUG] = live = snapshot(doc([task("a")]))
    rig.hub.poll()
    assert rig.claim(offered(live, "a")) is None
    rig.views[SLUG] = snapshot(doc([task("a")], freezes=[{"verb": "freeze", "target": "tasks/a"}]))
    rig.later()
    rig.hub.poll()
    assert rig.claim(offered(live, "a")) == {"state": "withdrawn", "by": "readiness"}


def test_a_task_claimed_on_the_ledger_after_its_offer_is_withdrawn(rig):
    rig.views[SLUG] = live = snapshot(doc([task("a"), task("b")]))
    rig.hub.poll()
    rig.views[SLUG] = snapshot(doc([task("a"), task("b")]), claimed={"a"})
    rig.later()
    rig.hub.poll()
    assert rig.claim(offered(live, "a")) == offers.WITHDRAWN
    assert rig.claim(offered(live, "b")) is None


def test_a_withdrawn_offer_is_forgotten_after_its_withdrawal(rig):
    rig.views[SLUG] = live = snapshot(doc([task("a")]))
    rig.hub.poll()
    rig.views[SLUG] = snapshot(doc([task("a", state="done")]))
    rig.later()
    rig.hub.poll()
    claims = rig.bus.state(offers.CLAIMS)
    entry = claims.get(claim_key(offered(live, "a")))
    claims.update(entry.key, json.dumps({"state": "ready"}).encode(), entry.revision)
    rig.later()
    rig.hub.poll()
    assert rig.claim(offered(live, "a")) == {"state": "ready"}
    assert rig.hub.outstanding(SLUG) == []


def test_a_spoke_owned_claim_is_left_alone_and_forgotten(rig):
    rig.views[SLUG] = live = snapshot(doc([task("a")]))
    rig.hub.poll()
    key = claim_key(offered(live, "a"))
    rig.bus.state(offers.CLAIMS).create(key, json.dumps({"owner": "hive", "state": "claimed"}).encode())
    rig.views[SLUG] = snapshot(doc([task("a", state="claimed")]))
    rig.later()
    rig.hub.poll()
    assert rig.claim(offered(live, "a")) == {"owner": "hive", "state": "claimed"}
    assert rig.hub.outstanding(SLUG) == []


def test_a_live_offer_stays_outstanding_across_passes(rig):
    rig.views[SLUG] = live = snapshot(doc([task("a")]))
    rig.hub.poll()
    rig.later()
    rig.hub.poll()
    assert [event.id for event in rig.hub.outstanding(SLUG)] == ["ready:sw:a:0"]
    assert rig.claim(offered(live, "a")) is None
    assert rig.queued() == ["ready:sw:a:0"]


def test_an_unfrozen_task_has_its_withdrawal_lifted(rig):
    rig.views[SLUG] = live = snapshot(doc([task("a")]))
    rig.hub.poll()
    rig.views[SLUG] = snapshot(doc([task("a")], freezes=[{"verb": "freeze", "target": "tasks/a"}]))
    rig.later()
    rig.hub.poll()
    rig.views[SLUG] = live
    rig.later()
    rig.hub.poll()
    assert rig.claim(offered(live, "a")) == {"state": "ready"}
    assert [event.id for event in rig.hub.outstanding(SLUG)] == ["ready:sw:a:0"]


def test_a_withdrawal_the_spoke_wrote_is_not_lifted(rig):
    rig.views[SLUG] = live = snapshot(doc([task("a")]))
    key = claim_key(offered(live, "a"))
    rig.bus.state(offers.CLAIMS).create(key, json.dumps({"state": "withdrawn"}).encode())
    rig.hub.poll()
    assert rig.claim(offered(live, "a")) == {"state": "withdrawn"}


def test_offer_returns_the_offers_it_placed_once_per_generation(rig):
    view = snapshot(doc([task("a"), task("b", lane="ci")]))
    assert rig.hub.offer(view, NOW) == ["ready:sw:a:0", "ready:sw:b:0"]
    assert rig.hub.offer(view, NOW) == []


def test_offers_use_the_given_queue_policy():
    clock = Clock()
    bus = memory.open_bus(BusConfig(), clock=clock)
    policy = QueuePolicy(max_deliver=2)
    hub = offers.Readiness(
        bus, SimpleNamespace(slugs=lambda: [SLUG]), lambda slug: snapshot(doc([task("a")])), clock, policy
    )
    hub.poll()
    assert bus.queue(readiness.queue_name(SLUG, "eng"), policy).policy == policy


def test_a_pass_runs_once_per_interval(rig):
    rig.hub.poll()
    rig.views[SLUG] = snapshot(doc([task("a")]))
    rig.clock.now += offers.INTERVAL_MS - 1
    rig.hub.poll()
    assert rig.queued() == []
    rig.clock.now += 1
    rig.hub.poll()
    assert rig.queued() == ["ready:sw:a:0"]


def test_a_lost_lease_stops_the_pass(rig):
    rig.views[SLUG] = snapshot(doc([task("a")]))
    rig.hub.poll(lambda: False)
    assert rig.queued() == []
    rig.hub.poll()
    assert rig.queued() == ["ready:sw:a:0"]


def test_a_lease_lost_between_swarms_stops_before_the_next(rig):
    rig.views[SLUG] = snapshot(doc([task("a")]))
    rig.views["tw"] = snapshot(doc([task("b")]), slug="tw")
    answers = iter([True, True, False])
    rig.hub.poll(lambda: next(answers))
    assert rig.queued(slug=SLUG) == ["ready:sw:a:0"]
    assert rig.queued(slug="tw") == []


def test_a_swarm_without_a_view_is_skipped(rig):
    rig.views["tw"] = None
    rig.views[SLUG] = snapshot(doc([task("a")]))
    rig.hub.poll()
    assert rig.queued() == ["ready:sw:a:0"]


def test_a_failing_swarm_does_not_stop_the_others(rig, capsys):
    good = snapshot(doc([task("a")]), slug="tw")

    def load(slug):
        if slug == SLUG:
            raise RuntimeError("ledger down")
        return good

    hub = offers.Readiness(rig.bus, SimpleNamespace(slugs=lambda: [SLUG, "tw"]), load, rig.clock)
    hub.poll()
    assert rig.queued(slug="tw") == ["ready:tw:a:0"]
    assert "hub: readiness pass for swarm sw failed" in capsys.readouterr().err


def test_a_failed_pass_waits_for_the_next_interval(rig):
    calls = []

    def load(slug):
        calls.append(slug)
        raise RuntimeError("ledger down")

    hub = offers.Readiness(rig.bus, rig.store, load, rig.clock)
    hub.poll()
    hub.poll()
    assert calls == [SLUG]


def test_the_memory_backend_without_a_store_runs_no_pass(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_BUS", raising=False)
    monkeypatch.setattr(offers, "connect", lambda: pytest.fail("connected"))
    hub = offers.Readiness(memory.open_bus(BusConfig()))
    hub.poll()


def test_a_missing_store_connects_and_reads_the_ledger(monkeypatch, rig):
    store = SimpleNamespace(slugs=lambda: [SLUG])
    ledger = object()
    seen = []
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.setattr(offers, "connect", lambda: store)
    monkeypatch.setattr(offers, "LedgerClient", lambda: ledger)
    monkeypatch.setattr(offers, "ledger_view", lambda *args: seen.append(args) or (lambda slug: None))
    hub = offers.Readiness(rig.bus, clock=rig.clock)
    hub.poll()
    assert seen == [(store, ledger)]


def test_the_ledger_view_reads_running_swarms_through_the_freeze_watch(monkeypatch):
    from scripts.swarm import freeze, tick

    found = doc([task("a")])
    configs = {"run": "running", "stop": "stopped"}
    store = SimpleNamespace(config=lambda slug: SimpleNamespace(state=configs[slug]))
    ledger = SimpleNamespace(state=lambda slug: {"raw": slug})
    monkeypatch.setattr(
        freeze, "watched", lambda *args: found if args == ("run", store, ledger, {"raw": "run"}) else None
    )
    monkeypatch.setattr(tick, "snapshot", lambda *args: args)
    load = offers.ledger_view(store, ledger)
    assert load("run") == ("run", store, {"a": found["tasks"][0]}, found)
    assert load("stop") is None


def test_the_offer_record_key_hashes_the_swarm():
    import hashlib

    assert offers.offers_key("sw") == hashlib.sha256(b"sw").hexdigest()


def test_the_reactor_is_a_singleton_poll_reactor(rig):
    reactor = rig.hub.reactor()
    assert (reactor.name, reactor.subject, reactor.singleton, reactor.poll) == (
        "readiness",
        "swarm.*.readiness.requested",
        True,
        rig.hub.poll,
    )
    assert reactor.handler(None) is None


def test_the_hub_registers_the_readiness_reactor():
    names = [reactor.name for reactor in registry.configured(memory.open_bus(BusConfig()))]
    assert "readiness" in names
