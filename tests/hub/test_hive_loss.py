import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit

T0 = 1_000_000


def reading():
    return {"cpus": 32, "load5": 0, "available_mb": 60000, "total_mb": 64000}


@pytest.fixture
def rig():
    import fakeredis

    from scripts.events import memory
    from scripts.events.config import BusConfig, QueuePolicy
    from scripts.events.envelope import Envelope
    from scripts.hive.spoke import Spoke, SpokeConfig
    from scripts.hub import hive_loss, lifecycle, registry, runner
    from scripts.swarm.store import PREFIX, AgentRecord, RedisStore
    from scripts.swarm_v2.runtime.base import Outcome, SpawnRequest, Status

    clock = Mock(return_value=T0)
    bus = memory.open_bus(BusConfig(), clock=clock)
    policy = QueuePolicy(max_deliver=3, backoff_ms=(10, 20))
    queue = bus.queue("swarm.rig.ready.eng", policy)

    def spoke(hive, name, execution, generation, ledger_row=lambda: {"claim_generation": 0}):
        from tests.swarm.test_readiness import doc, snapshot
        from tests.swarm.test_readiness import task as ready_task

        def view():
            return snapshot(doc([{**ready_task("task"), **ledger_row()}]))

        tasks, runtime = Mock(), Mock()
        task = {"id": "task", "execution_id": execution, "execution_generation": generation, "claim_generation": 3}
        tasks.request.return_value = SpawnRequest(SimpleNamespace(slug="rig"), "eng", name, task)
        runtime.spawn.return_value = Outcome(
            "spawn", Status.OK, "local", SimpleNamespace(pane_id=f"{hive}:{name}", harness="claude", account="a")
        )
        config = SpokeConfig(SimpleNamespace(slug="rig"), "eng", hive, policy)
        return SimpleNamespace(
            spoke=Spoke(config, bus, runtime, tasks, reading, clock=clock, view=view), runtime=runtime
        )

    def offer(version):
        queue.offer(
            Envelope(f"ready-{version}", "task.ready", "rig", "task", "task", version, T0, "readiness", {"lane": "eng"})
        )

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    agent = AgentRecord(
        name="agent-a",
        lane="eng",
        task="task",
        execution_id="execution-a",
        generation=7,
        seat="eng-1",
        account="a",
        hive="hive-a",
        runtime_target={"host_id": "hive-a"},
        started_at=T0,
        launched_at=T0,
        pane_id="hive-a:agent-a",
    )
    store.redis.sadd(f"{PREFIX}:index", "rig")
    store.redis.hset(store.key("rig", "executions"), agent.execution_id, json.dumps(asdict(agent)))
    store.redis.hset(store.key("rig", "agents"), agent.name, json.dumps(asdict(agent)))
    store.redis.set(store.key("rig", "claim", agent.task), agent.name)
    row = {"id": "task", "state": "claimed", "claimed_by": agent.name, "claim_generation": 3, "done": False}
    ledger = SimpleNamespace(task=lambda *_: dict(row), writes=[])

    def update(*args, **kwargs):
        ledger.writes.append((args, kwargs))
        row.update(args[2])

    ledger.update_task = update
    returned = []
    life = lifecycle.Lifecycle(bus, store, ledger, release=lambda *args: returned.append(args))
    loss = hive_loss.HiveLoss(bus, clock)
    hub = runner.HubRunner(bus, registry.Registry([loss.reactor(), life.reactor()]), "hub", clock)
    first = spoke("hive-a", "agent-a", "execution-a", 7)
    offer(0)
    yield SimpleNamespace(
        bus=bus,
        clock=clock,
        hub=hub,
        loss=loss,
        life=life,
        row=row,
        returned=returned,
        store=store,
        first=first,
        spoke=spoke,
        offer=offer,
        queue=queue,
    )
    hub.stop()
    bus.close()
    store.redis.close()


def advance(rig, to, beating=()):
    from scripts.hive import presence

    while rig.clock() < to:
        rig.clock.return_value = min(rig.clock() + 5_000, to)
        for hive in beating:
            presence.beat(rig.bus, hive, rig.clock())
        rig.hub.run_once()


def claim_state(rig):
    from scripts.events.envelope import Envelope
    from scripts.hive.spoke import claim_key

    key = claim_key(Envelope("ready-0", "task.ready", "rig", "task", "task", 0, T0, "readiness", {}))
    return json.loads(rig.bus.state("spoke-work").get(key).value)["state"]


def test_a_killed_hive_reopens_its_running_task_for_another_claim_at_the_presence_timeout(rig):
    from scripts.hive import presence

    assert rig.first.spoke.poll() == "started"
    rig.hub.run_once()
    advance(rig, T0 + presence.TTL_MS - 1)
    assert rig.row["state"] == "claimed"
    assert rig.returned == []
    advance(rig, T0 + presence.TTL_MS)
    assert rig.row == {**rig.row, "state": "open", "claimed_by": ""}
    assert len(rig.returned) == 1
    assert rig.store.agents("rig") == []
    assert claim_state(rig) == "exited"
    rig.offer(3)
    second = rig.spoke("hive-b", "agent-b", "execution-b", 8, lambda: rig.row)
    assert second.spoke.poll() == "started"
    second.runtime.spawn.assert_called_once()


def test_a_beating_hive_keeps_its_running_task(rig):
    from scripts.hive import presence

    assert rig.first.spoke.poll() == "started"
    rig.hub.run_once()
    advance(rig, T0 + 4 * presence.TTL_MS, beating=["hive-a"])
    assert rig.row["state"] == "claimed"
    assert rig.returned == []
    assert claim_state(rig) == "started"


def test_the_exit_is_the_one_exit_of_that_start_and_names_the_lost_hive(rig):
    from scripts.hive import execution_events, presence

    published = Mock(wraps=execution_events.emit)
    rig.loss._emit = published
    assert rig.first.spoke.poll() == "started"
    rig.hub.run_once()
    advance(rig, T0 + presence.TTL_MS + 10_000)
    exited = published.call_args.args[1]
    assert published.call_count == 1
    assert (exited.id, exited.type, exited.time, exited.cause) == (
        f"exited:{exited.cause}",
        "execution.exited",
        T0 + presence.TTL_MS,
        exited.cause,
    )
    assert exited.cause.startswith("started:")
    assert (exited.swarm, exited.key, exited.version) == ("rig", "execution-a", 7)
    assert exited.data["reason"] == "hive-lost"
    assert exited.data["hive"] == "hive-a"


def test_the_hub_logs_the_lost_hive_with_the_execution_it_exits(rig, capsys):
    from scripts.hive import presence

    assert rig.first.spoke.poll() == "started"
    rig.hub.run_once()
    advance(rig, T0 + presence.TTL_MS + 10_000)
    assert capsys.readouterr().err.splitlines() == [
        "hub: hive hive-a of swarm rig lost; exited execution execution-a of task task"
    ]


def test_the_hub_logs_every_execution_of_the_lost_hive_on_one_line(rig, capsys):
    from scripts.events.envelope import Envelope
    from scripts.hive import presence

    assert rig.first.spoke.poll() == "started"
    rig.hub.run_once()
    rig.bus.state("spoke-work").create("claim-b", json.dumps({"state": "started", "claim_revision": 1}).encode())
    data = {"claim_key": "claim-b", "claim_revision": 1, "hive": "hive-a", "task": "task-b"}
    rig.loss.handle(
        Envelope("started:b", "execution.started", "rig", "execution", "execution-b", 1, T0, "hive-a", data)
    )
    rig.loss._emit = Mock()
    advance(rig, T0 + presence.TTL_MS)
    assert capsys.readouterr().err.splitlines() == [
        "hub: hive hive-a of swarm rig lost; exited execution execution-a of task task, execution execution-b of task task-b"
    ]


def test_a_beating_hive_logs_no_loss(rig, capsys):
    from scripts.hive import presence

    assert rig.first.spoke.poll() == "started"
    rig.hub.run_once()
    advance(rig, T0 + 4 * presence.TTL_MS, beating=["hive-a"])
    assert capsys.readouterr().err == ""


def test_a_start_that_already_exited_is_dropped_without_an_exit(rig, capsys):
    from scripts.hive import presence
    from scripts.hub import hive_loss

    assert rig.first.spoke.poll() == "started"
    rig.hub.run_once()
    claims = rig.bus.state("spoke-work")
    from scripts.events.envelope import Envelope
    from scripts.hive.spoke import claim_key

    key = claim_key(Envelope("ready-0", "task.ready", "rig", "task", "task", 0, T0, "readiness", {}))
    entry = claims.get(key)
    claims.update(key, json.dumps({**json.loads(entry.value), "state": "exited"}).encode(), entry.revision)
    rig.loss._emit = Mock()
    advance(rig, T0 + presence.TTL_MS)
    rig.loss._emit.assert_not_called()
    assert json.loads(rig.bus.state(hive_loss.BUCKET).get(hive_loss.INDEX).value) == {}
    assert capsys.readouterr().err == ""


def test_a_live_hive_prunes_ended_starts_on_the_prune_cadence(rig):
    from scripts.events.envelope import Envelope
    from scripts.hive import presence
    from scripts.hive.spoke import claim_key
    from scripts.hub import hive_loss

    assert rig.first.spoke.poll() == "started"
    rig.hub.run_once()
    claims = rig.bus.state("spoke-work")
    key = claim_key(Envelope("ready-0", "task.ready", "rig", "task", "task", 0, T0, "readiness", {}))
    entry = claims.get(key)
    claims.update(key, json.dumps({**json.loads(entry.value), "claim_revision": 99}).encode(), entry.revision)
    index = rig.bus.state(hive_loss.BUCKET)
    rig.clock.return_value = T0 + hive_loss.PRUNE_MS - 1
    presence.beat(rig.bus, "hive-a", rig.clock())
    rig.hub.run_once()
    assert list(json.loads(index.get(hive_loss.INDEX).value)) == [key]
    rig.clock.return_value = T0 + hive_loss.PRUNE_MS
    presence.beat(rig.bus, "hive-a", rig.clock())
    rig.hub.run_once()
    assert json.loads(index.get(hive_loss.INDEX).value) == {}
    assert rig.returned == []


def test_polls_one_beat_apart_keep_the_watch(rig):
    from scripts.hive import presence
    from scripts.hub import hive_loss

    assert rig.first.spoke.poll() == "started"
    rig.hub.run_once()
    while rig.clock() < T0 + presence.TTL_MS:
        rig.clock.return_value = rig.clock() + hive_loss.GAP_MS
        rig.hub.run_once()
    assert rig.clock() == T0 + presence.TTL_MS
    assert rig.row["state"] == "open"


def test_a_gap_longer_than_a_beat_restarts_the_watch(rig):
    from scripts.hive import presence
    from scripts.hub import hive_loss

    assert rig.first.spoke.poll() == "started"
    rig.hub.run_once()
    resumed = T0 + 10 * presence.TTL_MS
    rig.clock.return_value = resumed - hive_loss.GAP_MS - 1
    rig.hub.run_once()
    rig.clock.return_value = resumed
    rig.hub.run_once()
    assert rig.row["state"] == "claimed"
    advance(rig, resumed + presence.TTL_MS - 1)
    assert rig.row["state"] == "claimed"
    advance(rig, resumed + presence.TTL_MS)
    assert rig.row["state"] == "open"


def test_a_denied_lease_does_nothing(rig):
    from scripts.hive import presence

    assert rig.first.spoke.poll() == "started"
    rig.hub.run_once()
    rig.clock.return_value = T0 + presence.TTL_MS
    rig.loss._emit = Mock()
    rig.loss.poll(lambda: False)
    rig.loss._emit.assert_not_called()


def test_starts_without_a_claim_or_hive_are_not_indexed(rig):
    from scripts.events.envelope import Envelope
    from scripts.hub import hive_loss

    data = {"claim_key": "c", "claim_revision": 2, "hive": "hive-a"}
    start = Envelope("started:x", "execution.started", "rig", "execution", "x", 1, T0, "hive-a", data)
    rig.loss.handle(replace(start, data={**data, "hive": ""}))
    rig.loss.handle(replace(start, data={"claim_revision": 2, "hive": "hive-a"}))
    rig.loss.handle(replace(start, type="execution.exited"))
    assert rig.bus.state(hive_loss.BUCKET).get(hive_loss.INDEX) is None
    rig.loss.handle(start)
    revision = rig.bus.state(hive_loss.BUCKET).get(hive_loss.INDEX).revision
    rig.loss.handle(replace(start, id="started:y", data={**data, "hive": "hive-b"}))
    assert rig.bus.state(hive_loss.BUCKET).get(hive_loss.INDEX).revision == revision
    rig.loss.handle(replace(start, id="started:z", data={**data, "claim_revision": 1, "hive": "hive-c"}))
    rows = json.loads(rig.bus.state(hive_loss.BUCKET).get(hive_loss.INDEX).value)
    assert rows == {"c": start.to_dict()}


def test_a_reclaim_of_the_same_claim_on_another_hive_replaces_the_watched_start(rig):
    from scripts.events.envelope import Envelope
    from scripts.hub import hive_loss

    data = {"claim_key": "c", "claim_revision": 2, "hive": "hive-a"}
    start = Envelope("started:x", "execution.started", "rig", "execution", "x", 1, T0, "hive-a", data)
    rig.loss.handle(start)
    reclaimed = replace(start, id="started:y", data={**data, "claim_revision": 3, "hive": "hive-b"})
    rig.loss.handle(reclaimed)
    rows = json.loads(rig.bus.state(hive_loss.BUCKET).get(hive_loss.INDEX).value)
    assert rows == {"c": reclaimed.to_dict()}


def test_default_registry_runs_hive_loss_before_lifecycle(rig):
    from scripts.hub import registry

    names = [reactor.name for reactor in registry.configured(rig.bus, rig.clock)]
    assert names.index("hive-loss") < names.index("lifecycle")
    reactor = next(r for r in registry.configured(rig.bus, rig.clock) if r.name == "hive-loss")
    assert (reactor.subject, reactor.singleton, callable(reactor.poll)) == (
        "swarm.*.execution.started",
        True,
        True,
    )
    assert reactor.poll.__self__._clock is rig.clock


def test_a_lease_lost_mid_pass_still_drops_the_starts_it_settled(rig):
    from scripts.events.envelope import Envelope
    from scripts.hive import presence
    from scripts.hub import hive_loss

    claims = rig.bus.state("spoke-work")
    for hive in ("hive-a", "hive-b"):
        presence.beat(rig.bus, hive, T0)
        claims.create(f"claim-{hive}", json.dumps({"state": "started", "claim_revision": 1}).encode())
        data = {"claim_key": f"claim-{hive}", "claim_revision": 1, "hive": hive, "task": hive}
        rig.loss.handle(Envelope(f"started:{hive}", "execution.started", "rig", "execution", hive, 1, T0, hive, data))
    while rig.clock() < T0 + hive_loss.PRUNE_MS:
        rig.clock.return_value = rig.clock() + 5_000
        rig.loss.poll(lambda: False)
    rig.loss._emit = Mock()
    rig.loss.poll(Mock(side_effect=[True, False]))
    assert [call.args[1].key for call in rig.loss._emit.call_args_list] == ["hive-a"]
    assert list(json.loads(rig.bus.state(hive_loss.BUCKET).get(hive_loss.INDEX).value)) == ["claim-hive-b"]
    assert rig.loss._pruned == T0
    rig.loss.poll()
    assert [call.args[1].key for call in rig.loss._emit.call_args_list] == ["hive-a", "hive-b"]
    assert json.loads(rig.bus.state(hive_loss.BUCKET).get(hive_loss.INDEX).value) == {}
    assert rig.loss._pruned == T0 + hive_loss.PRUNE_MS


def test_one_pass_prunes_a_live_hive_and_settles_a_lost_one(rig):
    from scripts.events.envelope import Envelope
    from scripts.hive import presence
    from scripts.hub import hive_loss

    claims = rig.bus.state("spoke-work")
    starts = [("running", "hive-a", "started"), ("ended", "hive-a", "exited"), ("lost", "hive-b", "started")]
    for key, hive, state in starts:
        presence.beat(rig.bus, hive, T0)
        claims.create(key, json.dumps({"state": state, "claim_revision": 1}).encode())
        data = {"claim_key": key, "claim_revision": 1, "hive": hive, "task": key}
        rig.loss.handle(Envelope(f"started:{key}", "execution.started", "rig", "execution", key, 1, T0, hive, data))
    while rig.clock() < T0 + hive_loss.PRUNE_MS:
        rig.clock.return_value = rig.clock() + 5_000
        rig.loss.poll(lambda: False)
    presence.beat(rig.bus, "hive-a", rig.clock())
    rig.loss._emit = Mock()
    rig.loss.poll()
    assert [call.args[1].key for call in rig.loss._emit.call_args_list] == ["lost"]
    assert list(json.loads(rig.bus.state(hive_loss.BUCKET).get(hive_loss.INDEX).value)) == ["running"]


def test_index_writes_retry_a_conflict_and_give_up_after_eight(rig):
    from scripts.events.bus import StateConflict
    from scripts.events.envelope import Envelope
    from scripts.hub import hive_loss

    store = rig.loss._state
    raced = [StateConflict("raced")]

    def create(key, value):
        if raced:
            raise raced.pop()
        return store.create(key, value)

    conflicts = Mock(side_effect=create)
    rig.loss._state = Mock(wraps=store, create=conflicts)
    data = {"claim_key": "c", "claim_revision": 1, "hive": "hive-a"}
    start = Envelope("started:x", "execution.started", "rig", "execution", "x", 1, T0, "hive-a", data)
    rig.loss.handle(start)
    assert json.loads(store.get(hive_loss.INDEX).value) == {"c": start.to_dict()}
    assert conflicts.call_count == 2
    rig.loss._state = Mock(wraps=store, update=Mock(side_effect=StateConflict("raced")))
    with pytest.raises(StateConflict) as raised:
        rig.loss.handle(replace(start, data={**data, "claim_revision": 2}))
    assert str(raised.value) == "hive loss index remained contended"
    assert rig.loss._state.update.call_count == 8
