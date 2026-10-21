import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def setup(monkeypatch):
    import fakeredis

    from scripts.hive.orders import Consumer, LaunchOrder, Orders, carried
    from scripts.swarm import launch_check, lease
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
    from scripts.swarm.tick import Placed
    from scripts.swarm_v2.runtime.base import Outcome, Status

    redis = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(redis)
    store.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    held = lease.acquire(store, "fixture", "controller")
    agent = store.start_execution(
        "fixture",
        AgentRecord(
            store.next_name("fixture", "eng"),
            "eng",
            "task",
            seat="eng-1@fixture",
            state="launching",
            harness="codex",
            profile="engineer",
            account="account",
            hive="host",
            runtime_target={"host_id": "host"},
        ),
    )
    redis.set("prompt", json.dumps(carried("fixture", {"id": "task", "description": "launch this task"}, "opening")))
    order = LaunchOrder(
        "fixture",
        agent.execution_id,
        agent.generation,
        "engineer",
        "eng",
        "task",
        "account",
        "codex",
        "subscription",
        "prompt",
        held.epoch,
        lease.now_ms(store) + 60000,
        "host",
    )
    placed = Placed(
        "pane",
        "codex",
        "account",
        model="selected-model",
        effort="high",
        profile="engineer",
        profile_decision={"validation": {"pid": 12}},
        launched_at=1234,
        overlays=["tuner"],
        launch_timings={"harness_at": 1300},
    )
    monkeypatch.setattr(launch_check, "declared", lambda profile, worn=(): [f"{profile}-host", *worn])
    runtime = Mock()
    runtime.spawn.return_value = Outcome("spawn", Status.OK, "local", placed)
    runtime.namespace.return_value = "namespace"
    runtime.table.return_value = {12: SimpleNamespace(start_time=34)}
    return SimpleNamespace(
        redis=redis,
        store=store,
        held=held,
        agent=agent,
        order=order,
        runtime=runtime,
        orders=Orders(redis),
        consumer=Consumer(redis, "host", runtime),
    )


def test_order_ack_and_live_execution(setup):
    item = setup.orders.send("host", setup.order)
    assert setup.redis.xrange(setup.orders.stream("host"))[0][0] == item
    assert setup.consumer.poll() == 1
    ack = setup.orders.ack(setup.order)
    assert ack.status == "live"
    assert ack.failure == ""
    assert ack.target == {"host_id": "host", "process_namespace": "namespace", "pid": 12, "pid_start": 34}
    assert setup.store.execution("fixture", setup.agent.execution_id) == setup.agent
    placed = setup.runtime.spawn.return_value.value
    assert ack.execution == {
        "pane_id": placed.pane_id,
        "model": placed.model,
        "effort": placed.effort,
        "profile_decision": {**placed.profile_decision, "host_overlays": ["engineer-host", "tuner"]},
        "launched_at": 1234,
        "launch_timings": {"harness_at": 1300},
        "overlays": ["tuner"],
    }
    request = setup.runtime.spawn.call_args.args[0]
    assert request.name == setup.agent.name
    assert request.lane == "eng"
    assert request.task["description"] == "launch this task"
    assert request.task["controller_epoch"] == setup.held.epoch
    assert request.task["route_kind"] == "subscription"
    assert request.config.lanes["eng"]["agent"] == "codex"
    assert request.config.lanes["eng"]["role"] == "engineer"
    config = setup.store.config("fixture")
    assert request.task["launch_assignment"] == {
        "profile": "engineer",
        "harness": "codex",
        "account": "account",
        "model": config.codex_model,
        "effort": config.effort_min,
    }
    assert not setup.redis.xrange(setup.orders.stream("host"))


@pytest.mark.parametrize(
    "field,value,failure", [("epoch", 0, "stale_epoch"), ("expires_at", 1, "expired"), ("generation", 9, "identity")]
)
def test_refusal_never_launches(setup, field, value, failure):
    order = replace(setup.order, **{field: value})
    setup.orders.send("host", order)
    assert setup.consumer.poll() == 1
    ack = setup.orders.ack(order)
    assert ack.status == "failed"
    assert ack.failure == failure
    assert ack.target == {}
    setup.runtime.spawn.assert_not_called()


def test_replay_returns_the_same_ack_without_another_launch(setup):
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    ack = setup.orders.ack(setup.order)
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    assert setup.orders.ack(setup.order) == ack
    setup.runtime.spawn.assert_called_once()
    setup.consumer.apply = Mock(side_effect=AssertionError("finished order was applied"))
    setup.orders.send("host", setup.order)
    setup.consumer.poll()


def test_crashed_launch_is_ambiguous_and_never_relaunched(setup):
    setup.runtime.spawn.side_effect = KeyboardInterrupt
    setup.orders.send("host", setup.order)
    with pytest.raises(KeyboardInterrupt):
        setup.consumer.poll()
    setup.runtime.spawn.side_effect = None
    setup.redis.hset(setup.orders.receipt(setup.order), "started", "1")
    setup.consumer.poll()
    ack = setup.orders.ack(setup.order)
    assert ack.status == "failed"
    assert ack.failure == "ambiguous"
    setup.runtime.spawn.assert_called_once()


def test_wrong_host_cannot_admit_execution(setup):
    from scripts.hive.orders import Consumer

    item = setup.orders.send("other", setup.order)
    Consumer(setup.redis, "other", setup.runtime).poll()
    assert json.loads(setup.redis.hget(f"{setup.orders.stream('other')}:acks", item)) == {
        "status": "failed",
        "target": {},
        "failure": "identity",
    }
    assert setup.orders.ack(setup.order) is None
    assert not setup.redis.xrange(setup.orders.stream("other"))
    setup.runtime.spawn.assert_not_called()


def test_runtime_failure_is_typed(setup):
    from scripts.swarm_v2.runtime.base import Outcome, Status

    setup.runtime.spawn.return_value = Outcome("spawn", Status.UNAVAILABLE, "local", detail="no launch slot")
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    ack = setup.orders.ack(setup.order)
    assert ack.status == "failed"
    assert ack.target == {}
    assert ack.failure == "unavailable: no launch slot"


def test_missing_prompt_is_refused(setup):
    setup.redis.delete("prompt")
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    ack = setup.orders.ack(setup.order)
    assert ack.status == "failed"
    assert ack.target == {}
    assert ack.failure == "prompt"
    setup.runtime.spawn.assert_not_called()


def test_host_identity_is_immutable_after_process_binding(setup):
    from scripts.swarm.store import SwarmError

    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    live = setup.store.execution("fixture", setup.agent.execution_id)
    with pytest.raises(SwarmError, match="immutable"):
        setup.store.put_agent("fixture", replace(live, runtime_target={**live.runtime_target, "host_id": "other"}))


def test_daemon_consumes_orders_after_its_heartbeat(setup, monkeypatch):
    from scripts.hive import daemon
    from scripts.hive.orders import Consumer

    monkeypatch.setattr(
        daemon, "observe", lambda **kwargs: {"slots": {}, "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    )
    monkeypatch.setattr(daemon, "execution_report", lambda *args: {"names": [], "panes": {}, "executions": {}})
    local = Mock(return_value=setup.consumer)
    monkeypatch.setattr(Consumer, "local", local)
    setup.orders.send("host", setup.order)
    daemon.beat(setup.redis, "host")
    local.assert_called_once_with(setup.redis, "host")
    assert setup.orders.ack(setup.order).status == "live"


@pytest.mark.parametrize(
    "field,value",
    [
        ("lane", "ci"),
        ("task", "different"),
        ("harness", "claude"),
        ("profile", "qa"),
        ("slot", "other"),
        ("route_kind", "interactive"),
        ("execution_id", "unknown"),
    ],
)
def test_changed_order_identity_is_refused(setup, field, value):
    order = replace(setup.order, **{field: value})
    setup.orders.send("host", order)
    setup.consumer.poll()
    assert setup.orders.ack(order).failure == "identity"
    setup.runtime.spawn.assert_not_called()


@pytest.mark.parametrize("raw", ["not json", "[]", "{}", '{"id":"other"}'])
def test_unusable_prompt_is_refused(setup, raw):
    setup.redis.set("prompt", raw)
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    ack = setup.orders.ack(setup.order)
    assert ack.status == "failed"
    assert ack.target == {}
    assert ack.failure == "prompt"
    setup.runtime.spawn.assert_not_called()


def test_inflight_replay_never_publishes_a_failure_over_a_live_launch(setup):
    from scripts.hive.orders import Consumer

    original = setup.runtime.spawn.return_value

    def overlap(request):
        assert Consumer(setup.redis, "host", setup.runtime).poll() == 1
        assert setup.orders.ack(setup.order) is None
        return original

    setup.runtime.spawn.side_effect = overlap
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    assert setup.orders.ack(setup.order).status == "live"
    setup.runtime.spawn.assert_called_once()


def test_started_order_expires_at_its_deadline_without_relaunch(setup, monkeypatch):
    from scripts.swarm import lease

    setup.orders.send("host", setup.order)
    setup.redis.hset(setup.orders.receipt(setup.order), "started", str(setup.order.expires_at - 60000))
    monkeypatch.setattr(lease, "now_ms", lambda store: setup.order.expires_at)
    setup.consumer.poll()
    ack = setup.orders.ack(setup.order)
    assert ack.status == "failed"
    assert ack.failure == "expired"
    assert ack.target == {}
    setup.runtime.spawn.assert_not_called()


def test_finish_publishes_the_canonical_ack_for_every_delivery(setup):
    from scripts.hive.orders import Ack

    first = setup.orders.send("host", setup.order)
    live = Ack("live", {"host_id": "host"})
    setup.orders.finish("host", first, setup.order, live)
    second = setup.orders.send("host", setup.order)
    setup.orders.finish("host", second, setup.order, Ack("failed", {}, "expired"))
    assert setup.orders.ack(setup.order) == live
    assert json.loads(setup.redis.hget(f"{setup.orders.stream('host')}:acks", second)) == {
        "status": "live",
        "target": {"host_id": "host"},
        "failure": "",
    }


@pytest.mark.parametrize("raw", ["not json", "{}", "[]"])
def test_malformed_delivery_is_acknowledged_and_does_not_block_the_stream(setup, raw):
    item = setup.redis.xadd(setup.orders.stream("host"), {"order": raw})
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    ack = json.loads(setup.redis.hget(f"{setup.orders.stream('host')}:acks", item))
    assert ack == {"status": "failed", "target": {}, "failure": "invalid_order"}
    assert not setup.redis.xrange(setup.orders.stream("host"))
    assert setup.orders.ack(setup.order).status == "live"


def test_missing_delivery_payload_is_acknowledged(setup):
    item = setup.redis.xadd(setup.orders.stream("host"), {"bad": "payload"})
    setup.consumer.poll()
    assert json.loads(setup.redis.hget(f"{setup.orders.stream('host')}:acks", item))["failure"] == "invalid_order"


def test_a_delivery_redis_refuses_is_acknowledged_refused_and_leaves_the_stream(setup, monkeypatch):
    from redis.exceptions import NoPermissionError

    item = setup.orders.send("host", setup.order)

    def refuse(order):
        raise NoPermissionError("No permissions to access a key")

    monkeypatch.setattr(setup.consumer, "apply", refuse)
    assert setup.consumer.poll() == 1
    ack = json.loads(setup.redis.hget(f"{setup.orders.stream('host')}:acks", item))
    assert ack == {"status": "failed", "target": {}, "failure": "refused"}
    assert not setup.redis.xrange(setup.orders.stream("host"))


def test_a_refused_receipt_write_for_an_acknowledged_order_still_leaves_the_stream(setup, monkeypatch):
    from redis.exceptions import NoPermissionError

    from scripts.hive.orders import Ack

    item = setup.orders.send("host", setup.order)
    setup.redis.hset(setup.orders.receipt(setup.order), "ack", json.dumps(asdict(Ack("live", {"host_id": "host"}))))

    def refuse(hive, entry, order, ack):
        raise NoPermissionError("No permissions to access a key")

    monkeypatch.setattr(setup.consumer.orders, "finish", refuse)
    assert setup.consumer.poll() == 1
    ack = json.loads(setup.redis.hget(f"{setup.orders.stream('host')}:acks", item))
    assert ack == {"status": "failed", "target": {}, "failure": "refused"}
    assert not setup.redis.xrange(setup.orders.stream("host"))


@pytest.mark.parametrize("field,value", [("harness", "claude"), ("profile", "qa"), ("account", "other")])
def test_runtime_substitution_is_refused_and_terminated(setup, field, value):
    placed = setup.runtime.spawn.return_value.value
    setup.runtime.spawn.return_value = replace(
        setup.runtime.spawn.return_value, value=replace(placed, **{field: value})
    )
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    assert setup.orders.ack(setup.order).failure == "identity"
    stopped = setup.runtime.terminate.call_args.args[0]
    assert stopped.execution_id == setup.agent.execution_id
    assert stopped.runtime_target == setup.orders.ack(setup.order).target
    assert setup.orders.ack(setup.order).status == "failed"
    setup.runtime.terminate.assert_called_once()


def test_launch_that_crosses_expiry_is_terminated(setup, monkeypatch):
    from scripts.swarm import lease

    original = setup.runtime.spawn.return_value

    def delayed(request):
        monkeypatch.setattr(lease, "now_ms", lambda store: setup.order.expires_at)
        return original

    setup.runtime.spawn.side_effect = delayed
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    ack = setup.orders.ack(setup.order)
    assert ack.status == "failed"
    assert ack.failure == "expired"
    stopped = setup.runtime.terminate.call_args.args[0]
    assert stopped.execution_id == setup.agent.execution_id
    assert stopped.runtime_target == setup.orders.ack(setup.order).target
    assert setup.orders.ack(setup.order).status == "failed"
    setup.runtime.terminate.assert_called_once()


def test_epoch_takeover_during_launch_refuses_live_admission(setup, monkeypatch):
    from scripts.swarm import lease
    from scripts.swarm.store import SwarmError

    original = setup.runtime.spawn.return_value

    def takeover(request):
        monkeypatch.setattr(lease, "require_epoch", Mock(side_effect=SwarmError("stale")))
        return original

    setup.runtime.spawn.side_effect = takeover
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    assert setup.orders.ack(setup.order).failure == "refused"
    stopped = setup.runtime.terminate.call_args.args[0]
    assert stopped.execution_id == setup.agent.execution_id
    assert stopped.runtime_target == setup.orders.ack(setup.order).target
    assert setup.orders.ack(setup.order).status == "failed"
    setup.runtime.terminate.assert_called_once()


@pytest.mark.parametrize("pid,table", [(None, {12: SimpleNamespace(start_time=34)}), (12, {})])
def test_unqualified_process_after_spawn_is_reported_ambiguous(setup, pid, table):
    placed = replace(setup.runtime.spawn.return_value.value, profile_decision={"validation": {"pid": pid}})
    setup.runtime.spawn.return_value = replace(setup.runtime.spawn.return_value, value=placed)
    setup.runtime.table.return_value = table
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    ack = setup.orders.ack(setup.order)
    assert ack.status == "failed"
    assert ack.failure == "ambiguous"
    assert ack.target == {"host_id": "host"}
    assert setup.store.execution("fixture", setup.agent.execution_id).state == "launching"


def test_failed_cleanup_is_reported_ambiguous(setup):
    from scripts.swarm_v2.runtime.base import Outcome, Status

    placed = replace(setup.runtime.spawn.return_value.value, profile="qa")
    setup.runtime.spawn.return_value = replace(setup.runtime.spawn.return_value, value=placed)
    setup.runtime.terminate.return_value = Outcome("terminate", Status.REFUSED, "local")
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    ack = setup.orders.ack(setup.order)
    assert ack.status == "failed"
    assert ack.failure == "ambiguous"
    stopped = setup.runtime.terminate.call_args.args[0]
    assert stopped.execution_id == setup.agent.execution_id
    assert stopped.runtime_target == setup.orders.ack(setup.order).target
    assert setup.orders.ack(setup.order).status == "failed"
    setup.runtime.terminate.assert_called_once()


def test_local_consumer_uses_the_selected_hive_and_runtime(setup, monkeypatch):
    from scripts.hive.orders import Consumer
    from scripts.swarm import runtime
    from scripts.swarm_v2.runtime import local

    herdr = Mock()
    adapter = Mock()
    monkeypatch.setattr(runtime, "HerdrRuntime", Mock(return_value=herdr))
    constructor = Mock(return_value=adapter)
    monkeypatch.setattr(local, "LocalHerdrRuntime", constructor)
    consumer = Consumer.local(setup.redis, "selected-host")
    assert consumer.hive == "selected-host"
    assert consumer.store.redis is setup.redis
    assert consumer.runtime is adapter
    constructor.assert_called_once_with(herdr)


def test_order_keys_isolate_hives_and_execution_receipts(setup):
    from scripts.hive import registry

    assert setup.orders.stream("host") == f"{registry.key('host')}:orders"
    assert setup.orders.stream("other") != setup.orders.stream("host")
    assert setup.orders.receipt(replace(setup.order, slug="other")) != setup.orders.receipt(setup.order)
    assert setup.orders.store.redis is setup.redis
    assert setup.orders.receipt(replace(setup.order, hive="other")) != setup.orders.receipt(setup.order)
    assert setup.orders.receipt(setup.order) == (
        f"{registry.key('host')}:launch-orders:fixture:{setup.order.execution_id}:{setup.order.generation}"
    )


def test_poll_consumes_only_a_bounded_batch(setup):
    for _ in range(33):
        setup.redis.xadd(setup.orders.stream("host"), {"order": "invalid"})
    assert setup.consumer.poll() == 32
    assert len(setup.redis.xrange(setup.orders.stream("host"))) == 1


@pytest.mark.parametrize("elapsed,expected", [(299999, None), (300000, "ambiguous"), (300001, "ambiguous")])
def test_interrupted_launch_waits_then_fails_at_the_deadline(setup, monkeypatch, elapsed, expected):
    from scripts.hive.orders import Ack
    from scripts.swarm import lease

    setup.redis.hset(setup.orders.receipt(setup.order), "started", "1000000")
    order = replace(setup.order, expires_at=2000000)
    monkeypatch.setattr(lease, "now_ms", lambda store: 1000000 + elapsed)
    ack = setup.consumer.apply(order)
    assert ack == (None if expected is None else Ack("failed", {}, expected))
    setup.runtime.spawn.assert_not_called()


def test_new_order_expires_at_its_exact_deadline(setup, monkeypatch):
    from scripts.hive.orders import Ack
    from scripts.swarm import lease

    monkeypatch.setattr(lease, "now_ms", lambda store: setup.order.expires_at)
    assert setup.consumer.apply(setup.order) == Ack("failed", {}, "expired")
    setup.runtime.spawn.assert_not_called()


def test_launch_claim_retains_the_server_timestamp(setup, monkeypatch):
    from scripts.swarm import lease

    now = setup.order.expires_at - 10000
    monkeypatch.setattr(lease, "now_ms", lambda store: now)
    setup.consumer.apply(setup.order)
    assert setup.redis.hget(setup.orders.receipt(setup.order), "started") == str(now)


def test_launch_refusal_has_a_typed_empty_target(setup):
    from scripts.hive.orders import Ack
    from scripts.swarm.store import SwarmError

    setup.runtime.spawn.side_effect = SwarmError("refused by runtime")
    assert setup.consumer.apply(setup.order) == Ack("failed", {}, "refused")


@pytest.mark.parametrize("occupant", [None, "replacement"])
def test_replaced_execution_is_refused_before_spawn(setup, monkeypatch, occupant):
    from scripts.swarm.store import SwarmError

    current = {} if occupant is None else {setup.agent.seat: replace(setup.agent, execution_id=occupant)}
    monkeypatch.setattr(setup.consumer.store.execution_registry, "occupants", lambda slug: current)
    with pytest.raises(SwarmError, match="^launch order names a replaced execution$"):
        setup.consumer.identity(setup.order, setup.agent)
    setup.runtime.spawn.assert_not_called()


def test_mismatched_admission_reports_the_identity_refusal(setup):
    from scripts.swarm.store import SwarmError

    with pytest.raises(SwarmError, match="^launch order does not match the admitted execution$"):
        setup.consumer.identity(replace(setup.order, generation=9), setup.agent)


def _outside(redis, prefix):
    readers = {
        "string": redis.get,
        "hash": redis.hgetall,
        "list": lambda key: redis.lrange(key, 0, -1),
        "set": redis.smembers,
        "zset": lambda key: redis.zrange(key, 0, -1, withscores=True),
        "stream": redis.xrange,
    }
    return {key: readers[redis.type(key)](key) for key in redis.keys("*") if not key.startswith(prefix)}


def test_a_joined_hive_applies_a_launch_order_writing_only_its_own_keys(setup):
    from scripts.hive import registry

    setup.orders.send("host", setup.order)
    before = _outside(setup.redis, registry.key("host"))
    assert setup.consumer.poll() == 1
    assert _outside(setup.redis, registry.key("host")) == before
    ack = setup.orders.ack(setup.order)
    assert ack.status == "live"
    assert ack.target == {"host_id": "host", "process_namespace": "namespace", "pid": 12, "pid_start": 34}
    assert ack.execution == {
        "pane_id": "pane",
        "model": "selected-model",
        "effort": "high",
        "profile_decision": {"validation": {"pid": 12}, "host_overlays": ["engineer-host", "tuner"]},
        "launched_at": 1234,
        "launch_timings": {"harness_at": 1300},
        "overlays": ["tuner"],
    }


def test_a_joined_hive_acknowledges_an_unknown_execution_when_redis_denies_the_conflict_count(setup, monkeypatch):
    from redis.exceptions import NoPermissionError

    from scripts.hive.orders import Ack

    def denied(*args, **kwargs):
        raise NoPermissionError("No permissions to access a key")

    order = replace(setup.order, execution_id="unknown")
    setup.orders.send("host", order)
    monkeypatch.setattr(setup.redis, "incr", denied)
    assert setup.consumer.poll() == 1
    assert setup.orders.ack(order) == Ack("failed", {}, "identity")
    setup.runtime.spawn.assert_not_called()


def test_a_refused_launch_terminates_the_placed_pane_and_process(setup):
    placed = setup.runtime.spawn.return_value.value
    setup.runtime.spawn.return_value = replace(setup.runtime.spawn.return_value, value=replace(placed, profile="qa"))
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    stopped = setup.runtime.terminate.call_args.args[0]
    assert stopped.runtime_target == {"host_id": "host", "process_namespace": "namespace", "pid": 12, "pid_start": 34}
    assert stopped.pane_id == "pane"


def test_an_order_for_another_hive_does_not_hold_back_the_rest_of_the_batch(setup):
    stray = setup.orders.send("host", replace(setup.order, hive="other"))
    setup.orders.send("host", setup.order)
    assert setup.consumer.poll() == 2
    assert json.loads(setup.redis.hget(f"{setup.orders.stream('host')}:acks", stray))["failure"] == "identity"
    assert setup.orders.ack(setup.order).status == "live"
    assert not setup.redis.xrange(setup.orders.stream("host"))


@pytest.mark.parametrize(
    "agent_model,agent_effort,lane_model,expected_model,expected_effort",
    [
        ("agent-model", "xhigh", "lane-model", "agent-model", "xhigh"),
        ("", "", "lane-model", "lane-model", "medium"),
    ],
)
def test_launch_keeps_lane_settings_and_selected_assignment(
    setup, agent_model, agent_effort, lane_model, expected_model, expected_effort
):
    agent = replace(setup.agent, model=agent_model, effort=agent_effort)
    setup.store.execution_registry.update("fixture", agent)
    config = setup.store.config("fixture")
    lane = {**config.lanes.get("eng", {}), "model": lane_model, "setting": "retained"}
    setup.store.update("fixture", lanes={**config.lanes, "eng": lane})
    setup.consumer.apply(setup.order)
    request = setup.runtime.spawn.call_args.args[0]
    assert request.config.lanes["eng"] == {**lane, "agent": "codex", "role": "engineer"}
    assert request.task["launch_assignment"] == {
        "profile": "engineer",
        "harness": "codex",
        "account": "account",
        "model": expected_model,
        "effort": expected_effort,
    }


def test_spawn_without_validation_is_reported_ambiguous(setup):
    from scripts.hive.orders import Ack

    placed = replace(setup.runtime.spawn.return_value.value, profile_decision={})
    setup.runtime.spawn.return_value = replace(setup.runtime.spawn.return_value, value=placed)
    assert setup.consumer.apply(setup.order) == Ack("failed", {"host_id": "host"}, "ambiguous")


@pytest.mark.parametrize(
    "status,detail,failure",
    [
        ("UNAVAILABLE", "directory does not exist: /x", "unavailable: directory does not exist: /x"),
        ("REFUSED", "", "refused"),
        ("AMBIGUOUS", "init-agent timed out", "ambiguous"),
    ],
)
def test_runtime_failure_carries_its_cause(setup, status, detail, failure):
    from scripts.hive.orders import Ack
    from scripts.swarm_v2.runtime.base import Outcome, Status

    setup.runtime.spawn.return_value = Outcome("spawn", Status[status], "local", detail=detail)
    assert setup.consumer.apply(setup.order) == Ack("failed", {}, failure)


def test_a_serena_router_that_does_not_come_up_fails_the_order_before_spawn(setup, monkeypatch):
    from scripts import serena_router_daemon
    from scripts.hive.orders import Ack

    monkeypatch.setattr(serena_router_daemon, "ensure", lambda: False)
    assert setup.consumer.apply(setup.order) == Ack("failed", {}, "serena")
    setup.runtime.spawn.assert_not_called()
