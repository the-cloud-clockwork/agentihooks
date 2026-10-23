import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def rig():
    import fakeredis

    from scripts.events import memory
    from scripts.events.config import BusConfig
    from scripts.events.envelope import Envelope
    from scripts.hive import execution_events, registry, reports
    from scripts.hub import lifecycle, reconciler
    from scripts.swarm.store import PREFIX, AgentRecord, RedisStore

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
    store.redis.sadd(f"{PREFIX}:index", "rig")
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


@pytest.mark.parametrize("sweep", ["_verify", "_reap"])
def test_legacy_tick_leaves_event_owned_execution_recovery_to_the_reconciler(rig, monkeypatch, sweep):
    from scripts.hub import reconciler
    from scripts.swarm import tick

    monkeypatch.setattr(reconciler.BusConfig, "from_environ", lambda: SimpleNamespace(backend="nats"))
    monkeypatch.setattr(reconciler, "open_bus", lambda _: rig.bus)
    runtime = Mock()
    runtime.live_names.return_value = set()
    runtime.bindings.return_value = {}
    assert getattr(tick, sweep)("rig", rig.store, rig.ledger, runtime, {"task": rig.row}, rig.clock()) == []
    runtime.observe.assert_not_called()
    runtime.retire.assert_not_called()
    assert rig.store.agents("rig") == [rig.agent]
    assert rig.row["state"] == "claimed"


def test_legacy_agents_are_unchanged_on_the_memory_backend(rig, monkeypatch):
    from scripts.hub import reconciler

    monkeypatch.setattr(reconciler.BusConfig, "from_environ", lambda: SimpleNamespace(backend="memory"))
    assert reconciler.legacy_agents(rig.store, "rig") == [rig.agent]


def test_unfenced_agents_remain_owned_by_legacy_tick(rig, monkeypatch):
    from scripts.hive import execution_events
    from scripts.hub import reconciler

    key = execution_events.identity("rig", "engineer", 3)
    state = rig.bus.state(execution_events.STARTS)
    entry = state.get(key)
    state.update(key, b"null", entry.revision)
    config = SimpleNamespace(backend="nats")
    monkeypatch.setattr(reconciler.BusConfig, "from_environ", lambda: config)
    opening = Mock(return_value=rig.bus)
    monkeypatch.setattr(reconciler, "open_bus", opening)
    assert reconciler.legacy_agents(rig.store, "rig") == [rig.agent]
    opening.assert_called_once_with(config)


@pytest.mark.parametrize("field", ["type", "swarm", "key", "version", "name", "task", "claim_key"])
def test_a_mismatched_stored_start_cannot_authorize_recovery(rig, field):
    from scripts.hive import execution_events

    raw = rig.event.to_dict()
    if field in {"name", "task", "claim_key"}:
        raw["data"][field] = "" if field == "claim_key" else "successor"
    else:
        raw[field] = 4 if field == "version" else "other"
    state = rig.bus.state(execution_events.STARTS)
    key = execution_events.identity("rig", "engineer", 3)
    entry = state.get(key)
    state.update(key, json.dumps(raw).encode(), entry.revision)
    rig.react.poll()
    assert rig.returned == []
    assert rig.row["state"] == "claimed"


@pytest.mark.parametrize("field", ["started_at", "launched_at", "event_time"])
def test_report_must_follow_every_recorded_launch_boundary(rig, field):
    from scripts.hive import execution_events

    if field == "event_time":
        event = replace(rig.event, time=960_000)
        state = rig.bus.state(execution_events.STARTS)
        key = execution_events.identity("rig", "engineer", 3)
        entry = state.get(key)
        state.update(key, json.dumps(event.to_dict()).encode(), entry.revision)
    else:
        agent = replace(rig.agent, **{field: 960_000})
        rig.store.redis.hset(rig.store.key("rig", "executions"), agent.execution_id, json.dumps(asdict(agent)))
    rig.report["observed_at"] = 950_000
    rig.save()
    rig.react.poll()
    assert rig.returned == []


@pytest.mark.parametrize("age,settled", [(300_000, True), (300_001, False)])
def test_report_freshness_has_a_five_minute_boundary(rig, age, settled):
    from scripts.hive import execution_events

    agent = replace(rig.agent, started_at=0, launched_at=0)
    rig.store.redis.hset(rig.store.key("rig", "executions"), agent.execution_id, json.dumps(asdict(agent)))
    state = rig.bus.state(execution_events.STARTS)
    key = execution_events.identity("rig", "engineer", 3)
    entry = state.get(key)
    state.update(key, json.dumps(replace(rig.event, time=0).to_dict()).encode(), entry.revision)
    rig.report["observed_at"] = rig.clock() - age
    rig.save()
    rig.react.poll()
    assert bool(rig.returned) is settled


def test_another_hive_cannot_report_this_execution_dead(rig):
    agent = replace(rig.agent, hive="other", runtime_target={"host_id": "other"})
    rig.store.redis.hset(rig.store.key("rig", "executions"), agent.execution_id, json.dumps(asdict(agent)))
    rig.react.poll()
    assert rig.returned == []


def test_host_target_identifies_a_hive_without_the_legacy_hive_field(rig):
    agent = replace(rig.agent, hive="")
    rig.store.redis.hset(rig.store.key("rig", "executions"), agent.execution_id, json.dumps(asdict(agent)))
    rig.react.poll()
    assert len(rig.returned) == 1


def test_absent_pane_without_recorded_pane_identity_is_not_an_exit(rig):
    agent = replace(rig.agent, pane_id="")
    rig.store.redis.hset(rig.store.key("rig", "executions"), agent.execution_id, json.dumps(asdict(agent)))
    rig.report.update(executions={}, pane_ids=[])
    rig.save()
    rig.react.poll()
    assert rig.returned == []


def test_a_denied_lease_performs_no_scan(rig):
    scan = Mock(wraps=rig.store.slugs)
    rig.store.slugs = scan
    rig.react.poll(lambda: False)
    scan.assert_not_called()
    assert rig.returned == []


def test_loss_of_lease_before_completion_does_not_record_the_pass(rig):
    rig.react.poll(Mock(side_effect=[True, True, False]))
    assert len(rig.returned) == 1
    scan = Mock(wraps=rig.store.slugs)
    rig.store.slugs = scan
    rig.react.poll()
    scan.assert_called_once_with()
    assert len(rig.returned) == 1


def test_a_request_delivery_cannot_repair_without_the_guarded_poll(rig):
    request = replace(rig.event, type="reconcile.requested")
    assert rig.react.reactor().handler(request) is None
    assert rig.returned == []
    rig.react.poll(lambda: False)
    assert rig.returned == []


def test_memory_default_registry_does_not_open_live_redis(rig, monkeypatch):
    from scripts.hub import reconciler

    connect = Mock(side_effect=AssertionError("must not open live Redis"))
    monkeypatch.setattr(reconciler, "connect", connect)
    monkeypatch.setattr(reconciler.BusConfig, "from_environ", lambda: SimpleNamespace(backend="memory"))
    reconciler.Reconciler(rig.bus, clock=rig.clock).poll()
    connect.assert_not_called()


@pytest.fixture
def restored(rig):
    from scripts.events import memory
    from scripts.events.config import BusConfig
    from scripts.hive import presence
    from scripts.hub import lifecycle, reconciler

    bus = memory.open_bus(BusConfig())
    presence.beat(bus, "hive", rig.clock())
    life = lifecycle.Lifecycle(bus, rig.store, rig.ledger, release=lambda *args: rig.returned.append(args))
    yield SimpleNamespace(bus=bus, react=reconciler.Reconciler(bus, store=rig.store, lifecycle=life, clock=rig.clock))
    bus.close()


def _stored_start(bus):
    from scripts.hive import execution_events

    entry = bus.state(execution_events.STARTS).get(execution_events.identity("rig", "engineer", 3))
    return json.loads(entry.value) if entry else None


def test_an_exited_execution_whose_start_a_restore_removed_is_rebuilt_and_settled(rig, restored):
    from scripts.hive import execution_events

    restored.react.poll()
    key = execution_events.identity("rig", "engineer", 3)
    start = _stored_start(restored.bus)
    assert (start["type"], start["swarm"], start["key"], start["version"]) == (
        "execution.started",
        "rig",
        "execution",
        3,
    )
    assert start["data"]["claim_key"] == key
    assert (start["data"]["task"], start["data"]["task_generation"], start["data"]["hive"]) == ("task", 7, "hive")
    assert json.loads(restored.bus.state("spoke-work").get(key).value)["state"] == "exited"
    assert rig.row["state"] == "open"
    assert rig.row["claimed_by"] == ""
    assert len(rig.returned) == 1
    assert rig.store.agents("rig") == []


def test_a_running_execution_whose_start_a_restore_removed_settles_on_its_later_exit(rig, restored):
    from scripts.hive import execution_events

    rig.report["executions"]["execution"]["pane"]["state"] = "running"
    rig.save()
    restored.react.poll()
    assert rig.row["state"] == "claimed"
    record = rig.report["executions"]["execution"]
    found = execution_events.initial(restored.bus, "hive", "execution", record, rig.clock())
    assert found.to_dict() == _stored_start(restored.bus)
    exited = replace(found, id=f"exited:{found.id}", type="execution.exited", cause=found.id)
    restored.react._lifecycle.handle(exited)
    assert rig.row["state"] == "open"
    assert len(rig.returned) == 1


def test_a_hive_that_never_pulled_work_keeps_its_unrecorded_execution_with_the_tick(rig):
    from scripts.events import memory
    from scripts.events.config import BusConfig
    from scripts.hub import reconciler

    bus = memory.open_bus(BusConfig())
    reconciler.Reconciler(bus, store=rig.store, lifecycle=rig.life, clock=rig.clock).poll()
    assert _stored_start(bus) is None
    assert rig.row["state"] == "claimed"
    bus.close()


@pytest.mark.parametrize("row", [None, {"state": "open"}, {"state": "claimed", "claimed_by": "successor"}])
def test_an_execution_the_ledger_no_longer_holds_is_not_rebuilt(rig, restored, row):
    rig.ledger.task = lambda *_: row
    restored.react.poll()
    assert _stored_start(restored.bus) is None
    assert rig.returned == []


def test_a_launch_still_in_progress_is_not_rebuilt(rig, restored):
    from scripts.hive import execution_events

    execution_events.prepare(restored.bus, "rig", "engineer", 3)
    restored.react.poll()
    assert _stored_start(restored.bus) is None
    assert rig.returned == []


def test_a_launch_placeholder_older_than_a_pass_interval_is_rebuilt(rig, restored):
    from scripts.hive import execution_events

    execution_events.prepare(restored.bus, "rig", "engineer", 3)
    rig.clock.return_value = 900_000 + 300_000
    rig.report["observed_at"] = rig.clock()
    rig.save()
    restored.react.poll()
    assert _stored_start(restored.bus)["data"]["claim_key"] == execution_events.identity("rig", "engineer", 3)
    assert rig.row["state"] == "open"
    assert len(rig.returned) == 1


def test_a_placeholder_one_millisecond_inside_the_interval_is_left(rig, restored):
    from scripts.hive import execution_events

    execution_events.prepare(restored.bus, "rig", "engineer", 3)
    rig.clock.return_value = 900_000 + 299_999
    rig.report["observed_at"] = rig.clock()
    rig.save()
    restored.react.poll()
    assert _stored_start(restored.bus) is None
    assert rig.returned == []


def test_a_rebuild_interrupted_after_its_claim_reuses_that_claim(rig, restored):
    from scripts.hive import execution_events

    key = execution_events.identity("rig", "engineer", 3)
    revision = restored.bus.state("spoke-work").create(key, json.dumps({"owner": "hive", "state": "claimed"}).encode())
    restored.react.poll()
    assert _stored_start(restored.bus)["data"]["claim_revision"] == revision
    assert rig.row["state"] == "open"
    assert len(rig.returned) == 1


def test_a_rebuild_interrupted_before_its_start_records_the_claimed_start(rig, restored):
    from scripts.hive import execution_events

    key = execution_events.identity("rig", "engineer", 3)
    claims = restored.bus.state("spoke-work")
    revision = claims.create(key, b"{}")
    event = replace(rig.event, id="rebuilt", data={**rig.event.data, "claim_key": key, "claim_revision": revision})
    claims.update(
        key, json.dumps({"state": "started", "claim_revision": revision, "event": event.to_dict()}).encode(), revision
    )
    restored.react.poll()
    assert _stored_start(restored.bus) == event.to_dict()
    assert rig.row["state"] == "open"
    assert len(rig.returned) == 1


def test_the_rebuilt_start_and_claim_carry_the_recorded_execution(rig, restored):
    from scripts.hive import execution_events

    calls, task = [], rig.ledger.task
    rig.ledger.task = lambda *args: calls.append(args) or task(*args)
    rig.report["executions"]["execution"]["pane"]["state"] = "running"
    rig.save()
    restored.react.poll()
    key = execution_events.identity("rig", "engineer", 3)
    entries = list(restored.bus.state("spoke-work").watch(key))
    start = _stored_start(restored.bus)
    assert calls == [("rig", "task")]
    assert [json.loads(entry.value) for entry in entries] == [
        {"owner": "hive", "state": "claimed"},
        {"state": "started", "claim_revision": entries[0].revision, "event": start},
    ]
    assert (start["id"], start["entity"], start["time"], start["author"]) == (
        f"started:{key}",
        "execution",
        900_000,
        "hub-reconciler",
    )
    assert start["data"] == {
        "task": "task",
        "task_generation": 7,
        "claim_key": key,
        "claim_revision": entries[0].revision,
        "generation": 3,
        "lane": "eng",
        "hive": "hive",
        "name": "engineer",
        "pane_id": "pane",
        "harness": "",
        "account": "a",
    }


def test_a_rebuild_finds_the_hive_through_the_host_target(rig, restored):
    agent = replace(rig.agent, hive="")
    rig.store.redis.hset(rig.store.key("rig", "executions"), agent.execution_id, json.dumps(asdict(agent)))
    rig.store.redis.hset(rig.store.key("rig", "agents"), agent.name, json.dumps(asdict(agent)))
    restored.react.poll()
    assert _stored_start(restored.bus)["data"]["hive"] == "hive"
    assert rig.row["state"] == "open"


def test_a_task_in_pull_request_is_rebuilt(rig, restored):
    rig.row["state"] = "pr"
    restored.react.poll()
    assert _stored_start(restored.bus)["data"]["task"] == "task"
    assert rig.row["state"] == "open"


def test_a_row_without_a_claim_generation_rebuilds_generation_zero(rig, restored):
    rig.ledger.task = lambda *_: {"state": "claimed", "claimed_by": "engineer"}
    restored.react.poll()
    assert _stored_start(restored.bus)["data"]["task_generation"] == 0


def test_default_reconciler_connects_lazily_for_the_due_pass(rig, monkeypatch):
    from scripts.hub import reconciler

    connect = Mock(return_value=rig.store)
    monkeypatch.setattr(reconciler, "connect", connect)
    building = Mock(return_value=rig.life)
    monkeypatch.setattr(reconciler, "Lifecycle", building)
    monkeypatch.setattr(reconciler.BusConfig, "from_environ", lambda: SimpleNamespace(backend="nats"))
    react = reconciler.Reconciler(rig.bus, clock=rig.clock)
    connect.assert_not_called()
    react.poll()
    assert len(rig.returned) == 1
    react.poll()
    connect.assert_called_once_with()
    building.assert_called_once_with(rig.bus, rig.store)


def test_an_empty_legacy_projection_never_opens_the_bus(rig, monkeypatch):
    from scripts.hub import reconciler

    monkeypatch.setattr(rig.store, "agents", lambda _: [])
    monkeypatch.setattr(reconciler.BusConfig, "from_environ", lambda: SimpleNamespace(backend="nats"))
    opening = Mock(side_effect=AssertionError("must not connect without agents"))
    monkeypatch.setattr(reconciler, "open_bus", opening)
    assert reconciler.legacy_agents(rig.store, "rig") == []
    opening.assert_not_called()


def test_the_reactor_listens_on_reconcile_requests_and_records_its_own_cadence(rig):
    from scripts.hub.reconciler import BUCKET, COMPLETED

    assert rig.react.reactor().subject == "swarm.*.reconcile.requested"
    rig.react.poll()
    assert rig.bus.state(BUCKET).get(COMPLETED).value == b"1000000"


def test_the_repair_settles_one_exit_derived_from_the_start_and_the_report(rig):
    handled = Mock(wraps=rig.life.handle)
    rig.life.handle = handled
    rig.react.poll()
    exited = handled.call_args.args[0]
    assert (exited.id, exited.type, exited.time, exited.cause) == (
        "exited:started",
        "execution.exited",
        1_000_000,
        "started",
    )
    assert (exited.swarm, exited.key, exited.version) == ("rig", "execution", 3)


def test_skipped_executions_ahead_in_the_scan_do_not_end_the_pass(rig):
    from scripts.hive import execution_events

    unfenced = replace(rig.agent, name="legacy", task="legacy", execution_id="unfenced", seat="eng-0", generation=1)
    elsewhere = replace(rig.agent, name="remote", task="remote", execution_id="remote", seat="eng-0", generation=2)
    elsewhere = replace(elsewhere, hive="other", runtime_target={"host_id": "other"})
    for agent in (unfenced, elsewhere):
        rig.store.redis.hset(rig.store.key("rig", "executions"), agent.execution_id, json.dumps(asdict(agent)))
    execution_events.started(
        rig.bus,
        replace(
            rig.event,
            id="remote-started",
            key="remote",
            version=2,
            data={**rig.event.data, "name": "remote", "task": "remote", "claim_key": "remote"},
        ),
    )
    assert [a.execution_id for a in rig.store.execution_registry.records("rig")] == [
        "unfenced",
        "remote",
        "execution",
    ]
    rig.react.poll()
    assert len(rig.returned) == 1
    assert rig.row["state"] == "open"


def test_the_default_registry_reconciler_uses_the_hub_clock(rig, monkeypatch):
    from scripts.hub import reconciler, registry

    monkeypatch.setattr(reconciler, "connect", Mock(return_value=rig.store))
    monkeypatch.setattr(reconciler, "Lifecycle", Mock(return_value=rig.life))
    monkeypatch.setattr(reconciler.BusConfig, "from_environ", lambda: SimpleNamespace(backend="nats"))
    reactors = {reactor.name: reactor for reactor in registry.configured(rig.bus, rig.clock)}
    reactors["reconciler"].poll()
    assert len(rig.returned) == 1
    assert rig.bus.state(reconciler.BUCKET).get(reconciler.COMPLETED).value == b"1000000"


def _stop_reporting(rig, expires_ms):
    from scripts.hive import presence, reports

    rig.store.redis.delete(reports.key("hive"))
    presence.beat(rig.bus, "hive", expires_ms - presence.TTL_MS)


def test_an_execution_on_a_hive_whose_presence_expired_is_settled_once_as_lost(rig):
    _stop_reporting(rig, 1_000_000)
    handled = Mock(wraps=rig.life.handle)
    rig.life.handle = handled
    rig.react.poll()
    rig.clock.return_value += 300_000
    rig.react.poll()
    assert len(rig.returned) == len(rig.ledger.writes) == 1
    assert rig.row["state"] == "open"
    lost = handled.call_args_list[0].args[0]
    assert (lost.id, lost.type, lost.time, lost.cause) == ("lost:started", "execution.exited", 1_000_000, "started")
    assert lost.data == {**rig.event.data, "reason": "lost"}
    assert rig.store.agents("rig") == []


def test_a_hive_that_reports_again_keeps_its_live_executions(rig):
    from scripts.hive import presence

    _stop_reporting(rig, 1_000_000)
    presence.beat(rig.bus, "hive", rig.clock())
    rig.report["executions"]["execution"]["pane"]["state"] = "working"
    rig.save()
    rig.react.poll()
    assert rig.returned == []
    assert rig.row["state"] == "claimed"
    assert rig.store.agents("rig") == [rig.agent]


def test_another_hive_losing_presence_cannot_settle_this_execution(rig):
    from scripts.hive import presence

    agent = replace(rig.agent, hive="other", runtime_target={"host_id": "other"})
    rig.store.redis.hset(rig.store.key("rig", "executions"), agent.execution_id, json.dumps(asdict(agent)))
    presence.beat(rig.bus, "other", 0)
    rig.react.poll()
    assert rig.returned == []
