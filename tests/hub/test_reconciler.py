import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group(name="fakeredis")]


@pytest.fixture
def rig():
    import fakeredis
    from scripts.bus import memory
    from scripts.bus.config import BusConfig

    from scripts.events.envelope import Envelope
    from scripts.hive import execution_events, registry, reports
    from scripts.hub import lifecycle, reconciler
    from scripts.swarm.store import AgentRecord, RedisStore

    clock = Mock(return_value=1_000_000)
    bus = memory.open_bus(BusConfig())
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    agent = AgentRecord(
        name="engineer",
        lane="eng",
        task="task",
        execution_id="execution",
        generation=3,
        seat="eng-1",
        account="a",
        hive="hive",
        runtime_target={"host_id": "hive"},
        started_at=900_000,
        launched_at=900_000,
        pane_id="pane",
    )
    store.redis.sadd("agentihooks:swarm:index", "rig")
    store.redis.hset(store.key("rig", "executions"), agent.execution_id, json.dumps(asdict(agent)))
    store.redis.hset(store.key("rig", "agents"), agent.name, json.dumps(asdict(agent)))
    store.redis.set(store.key("rig", "claim", agent.task), agent.name)
    row = {"id": "task", "state": "claimed", "claimed_by": agent.name, "claim_generation": 7, "done": False}
    ledger = SimpleNamespace(task=lambda *_: dict(row), writes=[])

    def update(*args, **kwargs):
        ledger.writes.append((args, kwargs))
        row.update(args[2])

    ledger.update_task = update
    returned = []
    life = lifecycle.Lifecycle(bus, store, ledger, release=lambda *args: returned.append(args))
    event = Envelope(
        id="started",
        type="execution.started",
        swarm="rig",
        entity="execution",
        key="execution",
        version=3,
        time=900_000,
        author="hive",
        data={
            "name": "engineer",
            "task": "task",
            "hive": "hive",
            "claim_key": "claim",
            "claim_revision": 1,
            "task_generation": 7,
        },
    )
    execution_events.started(bus, event)
    bus.state("spoke-work").create("claim", json.dumps({"state": "started", "claim_revision": 1}).encode())
    report = {
        "observed_at": clock(),
        "pane_ids": ["pane"],
        "executions": {"execution": {"slug": "rig", "name": "engineer", "generation": 3, "pane": {"state": "exited"}}},
    }

    def save_report():
        store.redis.sadd(registry.INDEX, "hive")
        store.redis.set(reports.key("hive"), json.dumps(report))

    save_report()
    react = reconciler.Reconciler(bus, store=store, lifecycle=life, clock=clock)
    yield SimpleNamespace(
        bus=bus,
        store=store,
        agent=agent,
        event=event,
        report=report,
        save=save_report,
        clock=clock,
        life=life,
        react=react,
        row=row,
        returned=returned,
        ledger=ledger,
    )
    bus.close()
    store.redis.close()


def test_fresh_exit_repairs_task_and_slot_without_an_exit_event(rig):
    rig.react.poll()
    assert rig.row["state"] == "open"
    assert rig.row["claimed_by"] == ""
    assert len(rig.returned) == 1
    assert rig.store.agents("rig") == []
    assert json.loads(rig.bus.state("spoke-work").get("claim").value)["state"] == "exited"


def test_repeated_pass_does_not_repeat_settlement(rig):
    rig.react.poll()
    rig.clock.return_value += 300_000
    rig.report["observed_at"] = rig.clock()
    rig.save()
    rig.react.poll()
    assert len(rig.returned) == len(rig.ledger.writes) == 1


def test_completed_task_closes_after_missed_exit(rig):
    rig.row["done"] = True
    rig.react.poll()
    assert rig.row["state"] == "done"


def test_cadence_is_exactly_five_minutes_and_survives_restart(rig):
    from scripts.hub.reconciler import Reconciler

    rig.react.poll()
    scan = Mock(wraps=rig.store.slugs)
    rig.store.slugs = scan
    restarted = Reconciler(rig.bus, store=rig.store, lifecycle=rig.life, clock=rig.clock)
    rig.clock.return_value += 299_999
    restarted.poll()
    scan.assert_not_called()
    rig.clock.return_value += 1
    restarted.poll()
    scan.assert_called_once_with()
    scan.reset_mock()
    restarted.poll()
    scan.assert_not_called()


def test_two_hub_replicas_share_singleton_and_durable_cadence(rig):
    from scripts.hub import reconciler, registry, runner

    other = reconciler.Reconciler(rig.bus, store=rig.store, lifecycle=rig.life, clock=rig.clock)
    one = runner.HubRunner(rig.bus, registry.Registry([rig.react.reactor()]), "one", rig.clock)
    two = runner.HubRunner(rig.bus, registry.Registry([other.reactor()]), "two", rig.clock)
    assert one.run_once() == {"reconciler": []}
    assert two.run_once() == {"reconciler": ["standby"]}
    scan = Mock(wraps=rig.store.slugs)
    rig.store.slugs = scan
    one.stop()
    assert two.run_once() == {"reconciler": []}
    scan.assert_not_called()
    rig.clock.return_value += 300_000
    two.run_once()
    scan.assert_called_once_with()
    two.stop()


@pytest.mark.parametrize(
    "change", ["missing", "stale", "future", "before_launch", "generation", "name", "swarm", "live", "unknown"]
)
def test_unproven_exit_preserves_recorded_execution(rig, change):
    from scripts.hive import reports

    record = rig.report["executions"]["execution"]
    if change == "missing":
        rig.store.redis.delete(reports.key("hive"))
    elif change == "stale":
        rig.report["observed_at"] = rig.clock() - 300_001
    elif change == "future":
        rig.report["observed_at"] = rig.clock() + 1
    elif change == "before_launch":
        rig.report["observed_at"] = 899_999
    elif change == "generation":
        record["generation"] = 2
    elif change == "name":
        record["name"] = "successor"
    elif change == "swarm":
        record["slug"] = "another"
    else:
        record["pane"]["state"] = "working" if change == "live" else "unknown"
    if change != "missing":
        rig.save()
    rig.react.poll()
    assert rig.row["state"] == "claimed"
    assert rig.returned == []
    assert rig.store.agents("rig") == [rig.agent]


def test_a_fresh_complete_report_can_prove_a_pane_disappeared(rig):
    rig.report["executions"] = {}
    rig.report["pane_ids"] = []
    rig.save()
    rig.react.poll()
    assert rig.row["state"] == "open"
    assert len(rig.returned) == 1


@pytest.mark.parametrize("panes", [None, ["pane"]])
def test_absence_without_complete_process_evidence_is_preserved(rig, panes):
    rig.report["executions"] = {}
    rig.report["pane_ids"] = panes
    rig.save()
    rig.react.poll()
    assert rig.returned == []


def test_legacy_executions_without_a_fenced_start_are_preserved(rig):
    from scripts.hive import execution_events

    key = execution_events.identity("rig", "engineer", 3)
    entry = rig.bus.state(execution_events.STARTS).get(key)
    raw = replace(rig.event, data={"name": "engineer", "hive": "hive"}).to_dict()
    rig.bus.state(execution_events.STARTS).update(key, json.dumps(raw).encode(), entry.revision)
    rig.react.poll()
    assert rig.returned == []


def test_lost_lease_stops_before_repairs_and_does_not_advance_cadence(rig):
    guard = Mock(side_effect=[True, False])
    rig.react.poll(guard)
    assert rig.returned == []
    rig.react.poll()
    assert len(rig.returned) == 1


def test_failed_settlement_retries_immediately_without_returning_slot_twice(rig, monkeypatch):
    update = rig.ledger.update_task
    monkeypatch.setattr(rig.ledger, "update_task", Mock(side_effect=ConnectionError("ledger unavailable")))
    with pytest.raises(ConnectionError):
        rig.react.poll()
    assert len(rig.returned) == 1
    monkeypatch.setattr(rig.ledger, "update_task", update)
    rig.react.poll()
    assert len(rig.returned) == 1
    assert rig.row["state"] == "open"


def test_default_registry_preserves_timers_and_lifecycle_and_registers_reconciler(rig):
    from scripts.hub import registry

    reactors = {reactor.name: reactor for reactor in registry.configured(rig.bus, rig.clock)}
    assert {"timers", "lifecycle", "reconciler"} <= reactors.keys()
    assert reactors["reconciler"].singleton is True
    assert callable(reactors["reconciler"].poll)
