import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def setup():
    import fakeredis

    from scripts.hive import launch, registry
    from scripts.swarm import lease
    from scripts.swarm.capacity import Account
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    config = SwarmConfig("fixture", "agentihooks", 1, 1)
    store.create(config)
    held = lease.acquire(store, config.slug, "controller")
    hive = registry._default("remote")
    hive.update(
        roles=["eng", "ci"],
        slots=[{"harness": "claude", "account": "account", "kind": "subscription"}],
        repos=[config.repo],
        heartbeat_at=lease.now_ms(store),
        max_agents=3,
    )
    store.redis.hset(registry.key("remote"), mapping={k: registry._encode(k, v) for k, v in hive.items()})
    store.redis.sadd(registry.INDEX, "remote")
    herdr = Mock()
    herdr._profile_decision.return_value = ProfileDecision("engineer", "task", "engineer")
    herdr._quota_accounts = [Account("claude", "account", "room", 0, 80, 80, 6)]
    herdr.planned_account.return_value = None
    launcher = launch.HiveLaunch(store, config.slug, held, herdr)
    return SimpleNamespace(store=store, config=config, held=held, hive=hive, herdr=herdr, launcher=launcher)


def test_resolves_profile_before_hive_selection(setup):
    from scripts.hive import launch, placement
    from scripts.swarm.profile_choice import ProfileDecision

    setup.herdr._profile_decision.return_value = ProfileDecision("qa", "classifier", "qa")
    selected = setup.launcher.select(setup.config, "eng", {"id": "task"})
    assert selected == placement.Wait(placement.NO_UI_HIVE)
    setup.herdr._profile_decision.assert_called_once()
    assert launch.ROLE_PROFILES["engineer"] == "eng"
    assert launch.ROLE_PROFILES["cicd"] == "ci"


def test_selects_an_advertised_claude_slot(setup):
    chosen = setup.launcher.select(setup.config, "eng", {"id": "task"})
    assert chosen.hive == "remote"
    assert chosen.account.name == "account"
    assert chosen.profile.profile == "engineer"
    assert chosen.route_kind == "subscription"


def test_codex_assignment_waits_for_its_own_placement_chain(setup):
    from scripts.hive.placement import Wait

    config = replace(setup.config, lanes={"eng": {"agent": "codex"}})
    assert setup.launcher.select(config, "eng", {"id": "task"}) == Wait("Codex hive placement is not enabled")


def test_no_advertised_slot_does_not_probe_controller_credentials(setup):
    from scripts.hive.placement import Wait

    setup.herdr._quota_accounts = []
    assert setup.launcher.select(setup.config, "eng", {"id": "task"}) == Wait("no advertised Claude account has room")
    setup.herdr.choose.assert_not_called()


def test_publishes_fenced_order_after_host_admission(setup):
    from scripts.hive.orders import Ack, Consumer, Orders
    from scripts.swarm.store import AgentRecord
    from scripts.swarm.tick import Placed
    from scripts.swarm_v2.runtime.base import Outcome, SpawnRequest, Status

    store, slug = setup.store, setup.config.slug
    name = store.next_name(slug, "eng")
    record = AgentRecord(name, "eng", "task", seat=f"eng-1@{slug}", state="starting")
    store.put_agent(slug, record)
    store.seats.occupy(record.seat, name, 0)
    task = {"id": "task", "seat": record.seat, "controller_epoch": setup.held.epoch}
    orders = Orders(store.redis)
    runtime = Mock()
    runtime.spawn.return_value = Outcome(
        "spawn",
        Status.OK,
        "local",
        Placed("pane", "claude", "account", profile="engineer", profile_decision={"validation": {"pid": 12}}),
    )
    runtime.namespace.return_value = "namespace"
    runtime.table.return_value = {12: SimpleNamespace(start_time=34)}
    consumer = Consumer(store.redis, "remote", runtime)
    setup.launcher.sleep = lambda _: consumer.poll()
    outcome = setup.launcher.spawn(SpawnRequest(setup.config, "eng", name, task))
    assert outcome.ok
    assert outcome.value.execution.runtime_target["host_id"] == "remote"
    assert outcome.value.execution.hive == "remote"
    assert outcome.value.execution.generation == 1
    assert outcome.value.execution.state == "working"
    assert runtime.spawn.call_count == 1
    order = setup.launcher.orders_for(task["id"])
    assert orders.ack(order) == Ack("live", outcome.value.execution.runtime_target)
    assert json.loads(store.redis.get(order.prompt_ref))["id"] == "task"


def test_local_mode_keeps_the_original_spawn_call(setup):
    from scripts.swarm.controller import FencedRuntime

    setup.herdr.spawn.return_value = "local result"
    fenced = FencedRuntime(setup.store, setup.config.slug, setup.held, setup.herdr, "local")
    task = {"id": "task"}
    assert fenced.spawn(setup.config, "eng", "name", task) == "local result"
    setup.herdr.spawn.assert_called_once_with(
        setup.config, "eng", "name", {**task, "controller_epoch": setup.held.epoch}
    )


@pytest.mark.parametrize("deployment", ["compose", "hybrid", "distributed"])
def test_nonlocal_controller_routes_worker_lanes_through_hives(setup, deployment):
    from scripts.swarm.controller import FencedRuntime
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    routed = RoutedRuntime(setup.herdr, RuntimeRouter([]))
    fenced = FencedRuntime(setup.store, setup.config.slug, setup.held, routed, deployment)
    assert fenced.router.placement.backend == "hive"
    assert fenced.has_capacity(setup.config)
    assert fenced.placement_refusal("eng", {"id": "task"}) == ""
    assert fenced.placement_refusal("master", {}) == "the workstation hive spawns master work"
    setup.herdr.spawn.assert_not_called()


def request_for(setup, name=None):
    from scripts.swarm.store import AgentRecord
    from scripts.swarm_v2.runtime.base import SpawnRequest

    name = name or setup.store.next_name(setup.config.slug, "eng")
    record = AgentRecord(name, "eng", "task", seat="eng-1@fixture", state="starting")
    setup.store.put_agent("fixture", record)
    setup.store.seats.occupy(record.seat, name, 0)
    return SpawnRequest(
        setup.config, "eng", name, {"id": "task", "seat": record.seat, "controller_epoch": setup.held.epoch}
    )


def order_for(setup, request, selected):
    from scripts.hive.orders import LaunchOrder

    admitted = setup.launcher.admit(request, selected)
    return LaunchOrder(
        "fixture",
        admitted.execution_id,
        admitted.generation,
        "engineer",
        "eng",
        "task",
        "account",
        "claude",
        "subscription",
        "prompt",
        setup.held.epoch,
        0,
    )


def test_disabled_hive_never_falls_back_to_local_spawn(setup):
    from scripts.swarm.controller import FencedRuntime
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    routed = RoutedRuntime(setup.herdr, RuntimeRouter([], disabled=["hive"]))
    fenced = FencedRuntime(setup.store, "fixture", setup.held, routed, "compose")
    assert fenced.placement_refusal("eng", {"id": "task"}) == "the hive runtime is disabled on this controller"
    setup.herdr.spawn.assert_not_called()


def test_expired_order_honors_a_definitive_receipt(setup):
    from scripts.hive.orders import Ack
    from scripts.swarm_v2.runtime.base import Status

    request = request_for(setup)
    selected = setup.launcher.select(setup.config, "eng", request.task)
    order = order_for(setup, request, selected)
    setup.launcher.slots.reserve("claude:account", order.execution_id, 6, 300000)
    setup.store.redis.hset(setup.store.key("fixture", "controller-launch-orders"), "task", json.dumps(order.__dict__))
    setup.launcher.orders.finish("remote", "0-0", order, Ack("failed", {}, "expired"))
    result = setup.launcher.wait(order)
    assert result.status is Status.REFUSED
    assert result.detail == "hive launch failed: expired"
    assert setup.launcher.orders_for("task") is None
    assert setup.store.execution_registry.get("fixture", order.execution_id).state == "ended"


def test_expired_unknown_order_remains_ambiguous(setup):
    from scripts.swarm_v2.runtime.base import Status

    request = request_for(setup)
    selected = setup.launcher.select(setup.config, "eng", request.task)
    order = order_for(setup, request, selected)
    result = setup.launcher.wait(order)
    assert result.status is Status.AMBIGUOUS
    assert result.detail == "hive launch acknowledgement expired"
    assert setup.store.execution_registry.get("fixture", order.execution_id).state == "launching"


def test_replay_refuses_another_claimant(setup):
    from scripts.hive.orders import Ack
    from scripts.swarm_v2.runtime.base import Status

    request = request_for(setup)
    order = order_for(setup, request, setup.launcher.select(setup.config, "eng", request.task))
    agent = setup.store.execution_registry.get("fixture", order.execution_id)
    setup.store.execution_registry.update("fixture", replace(agent, state="working"))
    setup.store.redis.hset(setup.store.key("fixture", "controller-launch-orders"), "task", json.dumps(order.__dict__))
    setup.launcher.orders.finish("remote", "0-0", order, Ack("live", agent.runtime_target))
    other = replace(request, name=setup.store.next_name("fixture", "eng"))
    result = setup.launcher.spawn(other)
    assert result.status is Status.REFUSED
    assert result.detail == "another claimant owns the hive launch order"
    assert len(setup.store.execution_registry.records("fixture")) == 1


def test_publication_uses_one_redis_transaction(setup):
    from unittest.mock import patch

    request = request_for(setup)
    setup.launcher.wait = Mock(return_value="pending")
    batches = []
    original = setup.store.redis.pipeline

    def pipeline(*args, **kwargs):
        pipe = original(*args, **kwargs)
        execute = pipe.execute

        def recorded(*args, **kwargs):
            batches.append([command[0][0] for command in pipe.command_stack])
            return execute(*args, **kwargs)

        pipe.execute = recorded
        return pipe

    with patch.object(setup.store.redis, "pipeline", side_effect=pipeline):
        assert setup.launcher.spawn(request) == "pending"
    queued = setup.store.redis.xrange(setup.launcher.orders.stream("remote"))
    assert len(queued) == 1
    assert json.loads(queued[0][1]["order"])["prompt_ref"] == setup.launcher.orders_for("task").prompt_ref
    assert ["SET", "HSET", "XADD"] in batches


def test_terminal_order_allows_a_new_claimant_and_generation(setup):
    request = request_for(setup)
    selected = setup.launcher.select(setup.config, "eng", request.task)
    order = order_for(setup, request, selected)
    agent = setup.store.execution_registry.get("fixture", order.execution_id)
    setup.store.execution_registry.update("fixture", replace(agent, state="finished"))
    setup.store.redis.hset(setup.store.key("fixture", "controller-launch-orders"), "task", json.dumps(order.__dict__))
    next_request = request_for(setup)
    setup.launcher.wait = Mock(return_value="pending")
    assert setup.launcher.spawn(next_request) == "pending"
    replacement = setup.store.execution_registry.get("fixture", setup.launcher.orders_for("task").execution_id)
    assert replacement.execution_id != agent.execution_id
    assert replacement.generation == agent.generation + 1
    assert replacement.name == next_request.name
    assert replacement.runtime_target == {"host_id": "remote"}


def test_reservation_contention_ends_the_unlaunched_execution(setup):
    from scripts.swarm_v2.runtime.base import Status

    request = request_for(setup)
    from unittest.mock import patch

    with patch.object(type(setup.launcher.slots), "reserve", return_value=False):
        result = setup.launcher.spawn(request)
    assert result.status is Status.UNAVAILABLE
    assert result.detail == "account reservation is full"
    assert setup.store.execution_registry.records("fixture")[0].state == "ended"
    assert setup.store.redis.xrange(setup.launcher.orders.stream("remote")) == []
    assert setup.launcher.orders_for("task") is None


def test_failed_order_can_retry_with_a_new_generation(setup):
    from scripts.hive.orders import Ack

    request = request_for(setup)
    order = order_for(setup, request, setup.launcher.select(setup.config, "eng", request.task))
    setup.store.redis.hset(setup.store.key("fixture", "controller-launch-orders"), "task", json.dumps(order.__dict__))
    setup.launcher.orders.finish("remote", "0-0", order, Ack("failed", {}, "prompt"))
    assert not setup.launcher.wait(order).ok
    next_request = request_for(setup)
    setup.launcher.wait = Mock(return_value="pending")
    assert setup.launcher.spawn(next_request) == "pending"
    assert setup.launcher.orders_for("task").generation == order.generation + 1
