import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.hub import lifecycle, registry, runner
from scripts.routing import reservation
from scripts.swarm.store import AgentRecord, RedisStore

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def rig():
    import fakeredis

    bus = memory.open_bus(BusConfig())
    agent = AgentRecord(
        name="engineer", lane="eng", task="task", execution_id="execution", generation=3, seat="eng-1", account="a"
    )
    row = {"id": "task", "state": "claimed", "claimed_by": agent.name, "claim_generation": 7, "done": False}
    ledger = SimpleNamespace(task=lambda *_: dict(row), writes=[])

    def update(*args, **kwargs):
        ledger.writes.append((args, kwargs))
        row.update(args[2])
        return dict(row)

    ledger.update_task = update
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.redis.hset(store.key("rig", "executions"), agent.execution_id, json.dumps(asdict(agent)))
    store.redis.hset(store.key("rig", "agents"), agent.name, json.dumps(asdict(agent)))
    store.redis.set(store.key("rig", "claim", agent.task), agent.name)
    returned = []
    event = Envelope(
        id="exit",
        type="execution.exited",
        swarm="rig",
        entity="execution",
        key="execution",
        version=3,
        time=100,
        author="hive",
        data={"name": agent.name, "task": "task", "claim_key": "claim", "claim_revision": 1, "task_generation": 7},
    )
    bus.state("spoke-work").create("claim", json.dumps({"state": "started", "claim_revision": 1}).encode())
    react = lifecycle.Lifecycle(bus, store, ledger, release=lambda _store, slug, agent: returned.append((slug, agent)))
    yield SimpleNamespace(
        bus=bus, agent=agent, row=row, store=store, ledger=ledger, returned=returned, event=event, react=react
    )
    bus.close()
    store.redis.close()


def test_exit_reopens_unfinished_work_and_returns_its_execution_slot(rig):
    rig.react.handle(rig.event)
    assert rig.row == {"id": "task", "state": "open", "claimed_by": "", "claim_generation": 7, "done": False}
    assert rig.ledger.writes == [
        (
            ("rig", "task", {"state": "open", "claimed_by": ""}),
            {"if_state": ("claimed", "pr"), "if_match": {"claimed_by": "engineer", "claim_generation": 7}},
        )
    ]
    assert rig.returned == [("rig", rig.agent)]
    assert rig.store.redis.get(rig.store.key("rig", "claim", "task")) is None
    assert rig.store.redis.hget(rig.store.key("rig", "agents"), "engineer") is None
    assert rig.store.execution_registry.get("rig", "execution").state == "ended"
    assert json.loads(rig.bus.state("spoke-work").get("claim").value)["state"] == "exited"


def test_exit_closes_a_task_with_an_authoritative_completion_proof(rig):
    rig.row.update(done=True)
    rig.react.handle(rig.event)
    assert rig.row["state"] == "done"
    assert rig.row["done"] is True
    assert rig.row["claimed_by"] == "engineer"
    assert rig.returned == [("rig", rig.agent)]


@pytest.mark.parametrize("state", ["done", "blocked", "handoff", "open"])
def test_exit_preserves_already_settled_tasks(rig, state):
    rig.row["state"] = state
    rig.react.handle(rig.event)
    assert rig.row["state"] == state
    assert rig.ledger.writes == []
    assert len(rig.returned) == 1


def test_exit_leaves_a_queued_pull_request_for_its_merge_reactor(rig):
    rig.row.update(state="pr", awaiting="merge")
    rig.react.handle(rig.event)
    assert rig.ledger.writes == []
    assert rig.row["state"] == "pr"
    assert len(rig.returned) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"key": "other"},
        {"entity": "task"},
        {"type": "execution.started"},
        {"data": {"name": "engineer", "task": "task", "claim_key": "claim", "claim_revision": 2}},
        {"data": {"name": "engineer", "task": "other", "claim_key": "claim", "claim_revision": 1}},
        {"data": {"name": "other", "task": "task", "claim_key": "claim", "claim_revision": 1}},
        {"data": {"name": "engineer", "task": "task", "claim_key": "missing", "claim_revision": 1}},
    ],
)
def test_stale_or_unrelated_exits_cannot_settle_or_return_another_slot(rig, change):
    rig.react.handle(replace(rig.event, **change))
    assert rig.ledger.writes == rig.returned == []


def test_exit_does_not_reopen_a_successors_task(rig):
    rig.row["claimed_by"] = "successor"
    rig.react.handle(rig.event)
    assert rig.ledger.writes == []
    assert rig.row["claimed_by"] == "successor"


def test_redelivery_does_not_repeat_task_or_slot_effects(rig):
    rig.react.handle(rig.event)
    lifecycle.Lifecycle(rig.bus, rig.store, rig.ledger, release=lambda *args: rig.returned.append(args)).handle(
        rig.event
    )
    assert len(rig.ledger.writes) == len(rig.returned) == 1


def test_failed_slot_return_retries_without_prematurely_reopening_the_task(rig):
    def unavailable(*_):
        raise OSError("slot store unavailable")

    failing = lifecycle.Lifecycle(rig.bus, rig.store, rig.ledger, release=unavailable)
    with pytest.raises(OSError, match="slot store unavailable"):
        failing.handle(rig.event)
    assert rig.ledger.writes == []
    rig.react.handle(rig.event)
    assert rig.row["state"] == "open"
    assert len(rig.returned) == 1


def test_the_hub_consumes_each_execution_version_once(rig):
    hub = runner.HubRunner(rig.bus, registry.Registry([rig.react.reactor()]), "hub")
    rig.bus.events.publish("swarm.rig.execution.exited", rig.event)
    assert hub.run_once() == {"lifecycle": ["handled"]}
    rig.bus.events.publish("swarm.rig.execution.exited", replace(rig.event, id="duplicate"))
    assert hub.run_once() == {"lifecycle": ["skipped"]}
    assert len(rig.returned) == len(rig.ledger.writes) == 1
    hub.stop()


def test_default_registry_wires_the_lifecycle_consumer(rig):
    reactors = {reactor.name: reactor for reactor in registry.configured(rig.bus)}
    reactor = reactors["lifecycle"]
    assert reactor.subject == "swarm.*.execution.exited"
    assert reactor.singleton is True
    assert isinstance(reactor.handler.__self__, lifecycle.Lifecycle)
    assert reactor.handler.__self__.bus is rig.bus


def test_a_ledger_outage_retries_settlement_without_returning_the_slot_again(rig):
    saved = rig.ledger.update_task

    def unavailable(*_, **__):
        raise OSError("ledger unavailable")

    rig.ledger.update_task = unavailable
    with pytest.raises(OSError, match="ledger unavailable"):
        rig.react.handle(rig.event)
    assert len(rig.returned) == 1
    rig.ledger.update_task = saved
    rig.react.handle(rig.event)
    assert rig.row["state"] == "open"
    assert len(rig.returned) == 1


def test_exit_accepts_the_launching_entry_revision_before_started_is_written(rig):
    claims = rig.bus.state("spoke-work")
    entry = claims.get("claim")
    revision = claims.update("claim", json.dumps({"state": "launching"}).encode(), entry.revision)
    rig.react.handle(replace(rig.event, data={**rig.event.data, "claim_revision": revision}))
    assert rig.row["state"] == "open"
    assert len(rig.returned) == 1


def test_the_default_slot_return_makes_the_account_available_again(rig):
    slots = reservation.LaunchSlots(rig.store.redis)
    assert slots.reserve("claude:a", "execution", 1, 10000)
    assert not slots.reserve("claude:a", "next", 1, 10000)
    lifecycle.Lifecycle(rig.bus, rig.store, rig.ledger).handle(rig.event)
    assert slots.reserve("claude:a", "next", 1, 10000)
    lifecycle.Lifecycle(rig.bus, rig.store, rig.ledger).handle(rig.event)
    assert not slots.reserve("claude:a", "third", 1, 10000)


def test_a_launching_exit_keeps_its_original_revision_after_a_slot_outage(rig):
    claims = rig.bus.state("spoke-work")
    entry = claims.get("claim")
    revision = claims.update("claim", json.dumps({"state": "launching"}).encode(), entry.revision)
    event = replace(rig.event, data={**rig.event.data, "claim_revision": revision})

    def unavailable(*_):
        raise OSError("unavailable")

    with pytest.raises(OSError, match="unavailable"):
        lifecycle.Lifecycle(rig.bus, rig.store, rig.ledger, release=unavailable).handle(event)
    rig.react.handle(event)
    assert rig.row["state"] == "open"
    assert len(rig.returned) == 1


def successor(rig):
    agent = replace(rig.agent, execution_id="successor", generation=4)
    rig.store.redis.hset(rig.store.key("rig", "executions"), agent.execution_id, json.dumps(asdict(agent)))
    rig.store.redis.hset(rig.store.key("rig", "agents"), agent.name, json.dumps(asdict(agent)))
    return agent


def test_a_delayed_exit_cannot_retire_a_same_name_successor(rig):
    replacement = successor(rig)
    rig.row["claim_generation"] = 8
    rig.react.handle(rig.event)
    assert rig.ledger.writes == rig.returned == []
    assert rig.store.execution_registry.get("rig", replacement.execution_id) == replacement
    assert rig.store.redis.hget(rig.store.key("rig", "agents"), replacement.name) is not None
    assert rig.store.redis.get(rig.store.key("rig", "claim", "task")) == replacement.name


def test_a_replacement_during_slot_return_aborts_the_whole_retirement(rig):
    slots = reservation.LaunchSlots(rig.store.redis)
    assert slots.reserve("claude:a", "execution", 1, 10000)
    replacements = []

    def race(store, slug, agent):
        lifecycle.release_slot(store, slug, agent)
        replacements.append(successor(rig))

    lifecycle.Lifecycle(rig.bus, rig.store, rig.ledger, release=race).handle(rig.event)
    assert len(replacements) == 1
    assert rig.ledger.writes == []
    assert not slots.reserve("claude:a", "other", 1, 10000)
    assert rig.store.execution_registry.get("rig", "successor") == replacements[0]
    assert rig.store.redis.get(rig.store.key("rig", "claim", "task")) == rig.agent.name


def test_a_new_task_claim_generation_is_never_reopened_by_the_old_exit(rig):
    rig.row["claim_generation"] = 8
    rig.react.handle(rig.event)
    assert rig.ledger.writes == []
    assert rig.row["state"] == "claimed"
    assert rig.row["claim_generation"] == 8
