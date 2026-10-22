import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.hub import lifecycle, registry, runner
from scripts.swarm.store import AgentRecord


@pytest.fixture
def rig():
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
    store = SimpleNamespace(execution_registry=SimpleNamespace(get=lambda *_: agent), released=[], dropped=[])
    store.release = lambda *args: store.released.append(args)
    store.drop_agent = lambda *args: store.dropped.append(args)
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
        data={"name": agent.name, "task": "task", "claim_key": "claim", "claim_revision": 1},
    )
    bus.state("spoke-work").create("claim", json.dumps({"state": "started", "claim_revision": 1}).encode())
    react = lifecycle.Lifecycle(bus, store, ledger, release=lambda *args: returned.append(args))
    yield SimpleNamespace(
        bus=bus, agent=agent, row=row, store=store, ledger=ledger, returned=returned, event=event, react=react
    )
    bus.close()


def test_exit_reopens_unfinished_work_and_returns_its_execution_slot(rig):
    rig.react.handle(rig.event)
    assert rig.row == {"id": "task", "state": "open", "claimed_by": "", "claim_generation": 7, "done": False}
    assert rig.ledger.writes == [
        (
            ("rig", "task", {"state": "open", "claimed_by": ""}),
            {"if_state": ("claimed", "pr"), "if_match": {"claimed_by": "engineer", "claim_generation": 7}},
        )
    ]
    assert rig.returned == [(rig.store, "rig", rig.agent)]
    assert rig.store.released == [("rig", "task", "engineer")]
    assert rig.store.dropped == [("rig", "engineer")]
    assert json.loads(rig.bus.state("spoke-work").get("claim").value)["state"] == "exited"


def test_exit_closes_a_task_with_an_authoritative_completion_proof(rig):
    rig.row.update(done=True)
    rig.react.handle(rig.event)
    assert rig.row["state"] == "done"
    assert rig.row["done"] is True
    assert rig.row["claimed_by"] == "engineer"
    assert rig.returned == [(rig.store, "rig", rig.agent)]


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
    assert rig.ledger.writes == rig.returned == rig.store.released == rig.store.dropped == []


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
    assert len(rig.ledger.writes) == len(rig.returned) == len(rig.store.released) == len(rig.store.dropped) == 1


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
