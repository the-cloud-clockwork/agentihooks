from types import SimpleNamespace

import pytest

from scripts.hub import preemption, registry
from scripts.inbox.store import InboxStore
from scripts.swarm import preempt
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.unit

SLUG = "sw"


def rows():
    return {
        "t1": {"id": "t1", "lane": "eng", "rank": "high", "state": "claimed", "claimed_by": "e1"},
        "t2": {"id": "t2", "lane": "eng", "state": "claimed", "claimed_by": "e2"},
        "t3": {"id": "t3", "lane": "eng", "rank": "urgent", "state": "open", "claimed_by": ""},
    }


@pytest.fixture
def rig():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", max_eng=2, max_ci=1, engine="events"))
    for name, task in (("e1", "t1"), ("e2", "t2")):
        store.put_agent(SLUG, AgentRecord(name, "eng", task, started_at=0))
        store.claim(SLUG, task, name, 60_000)
    found = rows()
    ledger = SimpleNamespace(
        metadata=lambda slug: {"_meta": {"rev": 1}},
        state=lambda slug: {"tasks": list(found.values()), "phases": [], "freezes": []},
    )
    now = SimpleNamespace(ms=1_000)
    reactor = preemption.Preemption(store, ledger, clock=lambda: now.ms)
    yield SimpleNamespace(store=store, reactor=reactor, now=now)
    store.redis.close()


def asks(store, name):
    return [item for item in InboxStore(store.redis).open_items(name) if item.ref.startswith(f"{preempt.REF}:")]


def poll_at(rig, *times):
    for at in times:
        rig.now.ms = at
        rig.reactor.poll()


def test_the_hub_registers_preemption_as_a_singleton_poll():
    from scripts.events.config import BusConfig
    from scripts.events.memory import open_bus

    bus = open_bus(BusConfig())
    try:
        found = {reactor.name: reactor for reactor in registry.configured(bus)}
    finally:
        bus.close()
    reactor = found["preemption"]
    assert reactor.singleton and reactor.poll is not None
    assert reactor.subject == "swarm.*.preemption.requested"


def test_an_urgent_task_still_waiting_a_tick_later_asks_the_lowest_holder_and_logs_it(rig, capsys):
    poll_at(rig, 1_000)
    assert asks(rig.store, "e1") == asks(rig.store, "e2") == []
    poll_at(rig, 1_000 + preemption.INTERVAL_MS)
    sent = asks(rig.store, "e2")
    assert len(sent) == 1 and asks(rig.store, "e1") == []
    assert sent[0].text == preempt.TEXT.format(slug=SLUG, waiting="t3", lane="eng", task="t2")
    line = f"hub: preemption swarm={SLUG}: asked e2 to hand off task t2 for urgent task t3 as message {sent[0].id}"
    assert capsys.readouterr().err.splitlines() == [line]


def test_polls_inside_one_tick_interval_run_preemption_once(rig):
    poll_at(rig, 1_000, 2_000, preemption.INTERVAL_MS)
    assert asks(rig.store, "e2") == []
    poll_at(rig, 1_000 + preemption.INTERVAL_MS)
    assert len(asks(rig.store, "e2")) == 1


@pytest.mark.parametrize("fields", [{"state": "paused"}, {"engine": "legacy"}])
def test_a_swarm_not_running_on_the_events_engine_asks_nobody(rig, fields):
    rig.store.update(SLUG, **fields)
    poll_at(rig, 1_000, 1_000 + preemption.INTERVAL_MS)
    assert asks(rig.store, "e1") == asks(rig.store, "e2") == []


def test_a_pass_stopped_by_a_lost_lease_runs_on_the_next_poll(rig):
    for at in (1_000, 1_000 + preemption.INTERVAL_MS):
        rig.now.ms = at
        rig.reactor.poll(lambda: False)
    assert asks(rig.store, "e2") == []
    poll_at(rig, 1_000 + preemption.INTERVAL_MS, 1_000 + 2 * preemption.INTERVAL_MS)
    assert len(asks(rig.store, "e2")) == 1


def test_an_unanswered_ask_passes_to_the_next_holder_after_the_answer_window(rig):
    asked_at = 1_000 + preemption.INTERVAL_MS
    poll_at(rig, 1_000, asked_at, asked_at + preemption.INTERVAL_MS)
    assert asks(rig.store, "e1") == []
    poll_at(rig, asked_at + preempt.ANSWER_MS)
    assert len(asks(rig.store, "e1")) == 1 and len(asks(rig.store, "e2")) == 1


def test_an_urgent_task_a_hive_refused_to_place_asks_nobody(rig):
    from scripts.hive import placement_waits

    placement_waits.observe(rig.store, SLUG, "eng", "t3", "no eng hive has room: cpu", 1_000)
    poll_at(rig, 1_000, 1_000 + preemption.INTERVAL_MS, 1_000 + 2 * preemption.INTERVAL_MS)
    assert asks(rig.store, "e1") == asks(rig.store, "e2") == []


def test_a_refused_ledger_write_skips_the_swarm_and_keeps_the_interval(rig, monkeypatch, capsys):
    from scripts.swarm.ledger_client import LedgerRefused

    def refused(*args):
        raise LedgerRefused("busy")

    monkeypatch.setattr(preempt, "run", refused)
    poll_at(rig, 1_000, 2_000)
    name = f"{refused.__module__}.{refused.__qualname__}"
    assert capsys.readouterr().err.splitlines() == [
        f"{name} skipped, the ledger refused its write: busy",
        f"hub: preemption swarm={SLUG}: skipped {name}: the ledger refused its write",
    ]


def test_the_memory_bus_without_a_store_runs_nothing(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_BUS", raising=False)
    reactor = preemption.Preemption(clock=lambda: 1_000)
    reactor.poll()
    assert reactor._source is None
