import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def setup():
    import fakeredis

    from scripts.hive import launch, registry
    from scripts.swarm import lease
    from scripts.swarm.capacity import Account
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    config = SwarmConfig("fixture", "agentihooks", 1, 1)
    store.create(config)
    held = lease.acquire(store, config.slug, "controller")
    hive = registry._default("remote")
    hive.update(
        slots=[{"harness": "claude", "account": "account", "kind": "subscription"}],
        repos=[config.repo],
        heartbeat_at=lease.now_ms(store),
        max_agents=3,
    )
    store.redis.hset(registry.key("remote"), mapping={k: registry._encode(k, v) for k, v in hive.items()})
    store.redis.sadd(registry.INDEX, "remote")
    herdr = Mock(spec=HerdrRuntime)
    herdr.choose = Mock()
    herdr._profile_decision.return_value = ProfileDecision("engineer", "task", "engineer")
    herdr._quota_accounts = [Account("claude", "account", "room", 0, 80, 80, 6)]
    herdr.planned_account.return_value = None
    launcher = launch.HiveLaunch(store, config.slug, held, herdr)
    launcher.opening = lambda request: f"opening for {request.name}"
    return SimpleNamespace(store=store, config=config, held=held, hive=hive, herdr=herdr, launcher=launcher)


def test_resolves_profile_before_hive_selection(setup):
    from scripts.hive import launch, placement
    from scripts.swarm.profile_choice import ProfileDecision

    setup.herdr._profile_decision.return_value = ProfileDecision("qa", "classifier", "qa")
    write_hive(setup, excluded=["qa"])
    selected = setup.launcher.select(setup.config, "eng", {"id": "task"})
    assert selected == placement.Wait("no live hive takes qa")
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


def test_publishes_fenced_order_after_host_admission(setup, monkeypatch):
    from scripts.hive.orders import Ack, Consumer, Orders
    from scripts.swarm import launch_check
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
        Placed(
            "pane",
            "claude",
            "account",
            model="selected-model",
            effort="high",
            profile="engineer",
            profile_decision={"validation": {"pid": 12}},
        ),
    )
    runtime.namespace.return_value = "namespace"
    runtime.table.return_value = {12: SimpleNamespace(start_time=34)}
    consumer = Consumer(store.redis, "remote", runtime)
    monkeypatch.setattr(launch_check, "declared", lambda profile, worn=(): [f"{profile}-host"])

    def consume(_):
        assert consumer.poll() == 1

    setup.launcher.sleep = consume
    outcome = setup.launcher.spawn(SpawnRequest(setup.config, "eng", name, task))
    assert outcome.ok
    assert outcome.value.execution.runtime_target["host_id"] == "remote"
    assert outcome.value.execution.hive == "remote"
    assert outcome.value.execution.generation == 1
    assert outcome.value.execution.state == "working"
    assert runtime.spawn.call_count == 1
    order = setup.launcher.orders_for(task["id"])
    assert orders.ack(order) == Ack(
        "live",
        outcome.value.execution.runtime_target,
        "",
        {
            "pane_id": "pane",
            "model": "selected-model",
            "effort": "high",
            "profile_decision": {"validation": {"pid": 12}, "host_overlays": ["engineer-host"]},
            "launched_at": 0,
            "launch_timings": {},
            "overlays": [],
        },
    )
    assert order.hive == "remote"
    assert json.loads(store.redis.get(order.prompt_ref)) == {
        **task,
        "profile_decision": setup.herdr._profile_decision.return_value.record(),
        "hive_opening": f"opening for {name}",
        "hive_opening_digest": hashlib.sha256(f"opening for {name}".encode()).hexdigest(),
        "hive_seeds": {},
    }
    assert order.prompt_ref == store.key(slug, f"launch-prompt:{order.execution_id}")
    assert 0 < store.redis.pttl(order.prompt_ref) <= 300000
    assert outcome.operation == "spawn"
    assert outcome.backend == "hive"
    assert outcome.value == Placed(
        "pane",
        "claude",
        "account",
        model="selected-model",
        effort="high",
        profile="engineer",
        profile_decision={"validation": {"pid": 12}, "host_overlays": ["engineer-host"]},
        execution=outcome.value.execution,
    )


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
    assert fenced.placement_refusal("master", {}).startswith("the workstation hive spawns master work: ")
    setup.herdr.spawn.assert_not_called()


def test_a_local_machine_swarm_with_hive_placement_sends_eng_to_a_joined_hive_and_the_master_home(setup):
    from scripts.swarm import controller
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    write_hive(setup, id="workstation", ui="yes", excluded=[])
    config = setup.store.update(setup.config.slug, hive_placement=True)
    deployment = controller.deployment_for(config, {"AGENTIHOOKS_DEPLOYMENT": "local"})
    routed = RoutedRuntime(setup.herdr, RuntimeRouter([]))
    fenced = controller.FencedRuntime(setup.store, config.slug, setup.held, routed, deployment)
    assert fenced.router.placement.backend == "hive"
    assert fenced.placement_refusal("eng", {"id": "task"}) == ""
    assert fenced.runtime.hive_launch.select(config, "eng", {"id": "task"}).hive == "remote"
    setup.herdr._profile_decision.return_value = ProfileDecision("master", "lane", "master")
    assert fenced.runtime.hive_launch.select(config, "master", {"id": "master"}).hive == "workstation"
    setup.herdr.spawn.assert_not_called()


def test_a_master_pinned_to_a_hive_is_placed_there_through_the_hive_runtime(setup):
    from scripts.hive.placement import Wait
    from scripts.swarm.controller import FencedRuntime
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    setup.herdr._profile_decision.return_value = ProfileDecision("master", "lane", "master")
    for hive in ("first", "second"):
        write_hive(setup, id=hive, ui="yes", excluded=[])
    pinned = {"id": "master", "hive": "second"}
    assert setup.launcher.select(setup.config, "master", {"id": "master"}).hive == "first"
    assert setup.launcher.select(setup.config, "master", pinned).hive == "second"
    assert setup.launcher.select(setup.config, "master", {"id": "master", "hive": "gone"}) == Wait(
        "no live hive takes master"
    )

    fenced = FencedRuntime(
        setup.store, setup.config.slug, setup.held, RoutedRuntime(setup.herdr, RuntimeRouter([])), "compose"
    )
    assert fenced.placement_refusal("master", pinned) == ""
    gone = fenced.placement_refusal("master", {"id": "master", "hive": "gone"})
    assert gone == "the workstation hive spawns master work: no live hive takes master"
    fenced.runtime.spawn = Mock(return_value="placed on the hive")
    fenced.runtime.original_runtime = Mock()
    assert fenced.spawn(setup.config, "master", "name", pinned) == "placed on the hive"
    fenced.runtime.spawn.assert_called_once_with(
        setup.config, "master", "name", {**pinned, "controller_epoch": setup.held.epoch}
    )
    fenced.runtime.original_runtime.spawn.assert_not_called()


def request_for(setup, name=None, lane="eng", route="subscription"):
    from scripts.swarm.store import AgentRecord
    from scripts.swarm_v2.runtime.base import SpawnRequest

    name = name or setup.store.next_name(setup.config.slug, lane)
    record = AgentRecord(name, lane, "task", seat=f"{lane}-1@fixture", state="starting")
    setup.store.put_agent("fixture", record)
    setup.store.seats.occupy(record.seat, name, 0)
    return SpawnRequest(
        setup.config,
        lane,
        name,
        {
            "id": "task",
            "title": "Launch on a hive",
            "seat": record.seat,
            "controller_epoch": setup.held.epoch,
            "route_kind": route,
        },
    )


def order_for(setup, request, selected):
    from scripts.hive.orders import LaunchOrder

    admitted = setup.launcher.admit(request, selected)
    return LaunchOrder(
        setup.config.slug,
        admitted.execution_id,
        admitted.generation,
        selected.profile.profile,
        request.lane,
        request.task["id"],
        selected.account.name,
        "claude",
        selected.route_kind,
        "prompt",
        setup.held.epoch,
        0,
        selected.hive,
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
    assert result.operation == "spawn"
    assert result.backend == "hive"
    assert json.loads(setup.store.redis.hget(setup.store.key("fixture", "launch-endings"), order.execution_id)) == {
        "reason": "expired",
        "generation": order.generation,
    }
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
    assert result.operation == "spawn"
    assert result.backend == "hive"
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
    assert result.operation == "spawn"
    assert result.backend == "hive"
    assert len(setup.store.execution_registry.records("fixture")) == 1


def test_expired_order_of_a_retired_claimant_is_ended_and_replaced(setup):
    request = request_for(setup)
    order = order_for(setup, request, setup.launcher.select(setup.config, "eng", request.task))
    agent = setup.store.execution_registry.get("fixture", order.execution_id)
    setup.store.execution_registry.update("fixture", replace(agent, state="working"))
    setup.store.redis.hset(setup.store.key("fixture", "controller-launch-orders"), "task", json.dumps(order.__dict__))
    setup.store.drop_agent("fixture", request.name)
    assert setup.store.execution_registry.get("fixture", order.execution_id).state == "ended"
    other = request_for(setup)
    setup.launcher.wait = Mock(return_value="pending")
    assert setup.launcher.spawn(other) == "pending"
    assert setup.store.execution_registry.get("fixture", order.execution_id).state == "ended"
    assert setup.store.redis.hget(setup.store.key("fixture", "launch-endings"), order.execution_id) is None
    replacement = setup.launcher.orders_for("task")
    assert replacement.execution_id != order.execution_id
    assert setup.store.execution_registry.get("fixture", replacement.execution_id).name == other.name


def test_expired_order_of_a_departed_claimant_left_working_is_abandoned(setup):
    request = request_for(setup)
    order = order_for(setup, request, setup.launcher.select(setup.config, "eng", request.task))
    agent = setup.store.execution_registry.get("fixture", order.execution_id)
    setup.store.execution_registry.update("fixture", replace(agent, state="working"))
    setup.store.redis.hset(setup.store.key("fixture", "controller-launch-orders"), "task", json.dumps(order.__dict__))
    setup.store.redis.hdel(setup.store.key("fixture", "agents"), request.name)
    other = request_for(setup)
    setup.launcher.wait = Mock(return_value="pending")
    assert setup.launcher.spawn(other) == "pending"
    assert setup.store.execution_registry.get("fixture", order.execution_id).state == "ended"
    assert json.loads(setup.store.redis.hget(setup.store.key("fixture", "launch-endings"), order.execution_id)) == {
        "reason": "hive launch order abandoned",
        "generation": order.generation,
    }
    replacement = setup.launcher.orders_for("task")
    assert replacement.execution_id != order.execution_id


def test_an_order_is_abandoned_from_its_expiry_instant_only_without_its_claimant(setup, monkeypatch):
    from scripts.hive import launch

    request = request_for(setup)
    order = order_for(setup, request, setup.launcher.select(setup.config, "eng", request.task))
    previous = setup.store.execution_registry.get("fixture", order.execution_id)
    monkeypatch.setattr(launch.lease, "now_ms", lambda store: order.expires_at)
    assert not setup.launcher.abandoned(order, previous)
    setup.store.drop_agent("fixture", request.name)
    assert setup.launcher.abandoned(order, previous)
    monkeypatch.setattr(launch.lease, "now_ms", lambda store: order.expires_at - 1)
    assert not setup.launcher.abandoned(order, previous)


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
    assert result.operation == "spawn"
    assert result.backend == "hive"
    ended = setup.store.execution_registry.records("fixture")[0]
    assert ended.state == "ended"
    assert json.loads(setup.store.redis.hget(setup.store.key("fixture", "launch-endings"), ended.execution_id)) == {
        "reason": "account reservation is full",
        "generation": ended.generation,
    }
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


def write_hive(setup, **changes):
    from scripts.hive import registry

    hive = {**setup.hive, **changes}
    setup.store.redis.hset(
        registry.key(hive["id"]), mapping={key: registry._encode(key, value) for key, value in hive.items()}
    )
    setup.store.redis.sadd(registry.INDEX, hive["id"])


def live_order(setup, route="subscription"):
    from unittest.mock import patch

    from scripts.hive.orders import Consumer, carried
    from scripts.swarm import launch_check, lease
    from scripts.swarm.tick import Placed
    from scripts.swarm_v2.runtime.base import Outcome, Status

    if route == "interactive":
        write_hive(setup, interactive={"claude": "account", "codex": ""})
    request = request_for(setup, route=route)
    selected = setup.launcher.select(setup.config, "eng", request.task)
    order = replace(order_for(setup, request, selected), expires_at=lease.now_ms(setup.store) + 60000)
    setup.store.redis.set("prompt", json.dumps(carried("fixture", request.task, "opening")))
    setup.store.redis.hset(setup.store.key("fixture", "controller-launch-orders"), "task", json.dumps(order.__dict__))
    setup.launcher.slots.reserve("claude:account", order.execution_id, 6, 300000)
    runtime = Mock()
    runtime.spawn.return_value = Outcome(
        "spawn",
        Status.OK,
        "local",
        Placed(
            "pane",
            "claude",
            "account",
            model="selected-model",
            effort="high",
            profile="engineer",
            profile_decision={"validation": {"pid": 12}},
            launched_at=1234,
            overlays=["tuner"],
            launch_timings={"harness_at": 1300},
        ),
    )
    runtime.namespace.return_value = "namespace"
    runtime.table.return_value = {12: SimpleNamespace(start_time=34)}
    consumer = Consumer(setup.store.redis, "remote", runtime)
    with patch.object(launch_check, "declared", lambda profile, worn=(): list(worn)):
        ack = consumer.apply(order)
    assert ack.status == "live"
    setup.launcher.orders.finish("remote", "0-0", order, ack)
    return request, order, ack


@pytest.mark.parametrize("route", ["subscription", "interactive"])
def test_acknowledgement_preserves_the_complete_launch_metadata(setup, route):
    from scripts.swarm.tick import Placed
    from scripts.swarm_v2.runtime.base import Outcome, Status

    _, order, ack = live_order(setup, route)
    result = setup.launcher.complete(order, ack)
    live = setup.store.execution_registry.get("fixture", order.execution_id)
    assert result == Outcome(
        "spawn",
        Status.OK,
        "hive",
        Placed(
            "pane",
            "claude",
            "account",
            model="selected-model",
            effort="high",
            profile="engineer",
            profile_decision={"validation": {"pid": 12}, "host_overlays": ["tuner"]},
            route_kind=route,
            execution=live,
            launched_at=1234,
            overlays=["tuner"],
            launch_timings={"harness_at": 1300},
        ),
    )
    assert (live.launched_at, live.overlays, live.launch_timings) == (1234, ["tuner"], {"harness_at": 1300})


@pytest.mark.parametrize(
    "bad", ["target", "pane", "host", "generation", "state", "missing occupant", "replaced occupant"]
)
def test_live_acknowledgement_refuses_inconsistent_execution_identity(setup, bad):
    from unittest.mock import patch

    from scripts.swarm.store import SwarmError

    _, order, ack = live_order(setup)
    live = setup.store.execution_registry.get("fixture", order.execution_id)
    current = setup.store.execution_occupants("fixture")
    if bad == "target":
        setup.launcher.complete(order, ack)
        ack = replace(ack, target={**ack.target, "pid": 99})
    elif bad == "pane":
        setup.launcher.complete(order, ack)
        ack = replace(ack, execution={**ack.execution, "pane_id": "other"})
    elif bad == "host":
        ack = replace(ack, target={**ack.target, "host_id": "elsewhere"})
    elif bad == "generation":
        order = replace(order, generation=order.generation + 1)
    elif bad == "state":
        setup.store.execution_registry.update("fixture", replace(live, state="suspect"))
    elif bad == "missing occupant":
        current = {}
    else:
        current = {live.seat: replace(live, execution_id="other")}
    with patch.object(setup.store.execution_registry, "occupants", return_value=current):
        with pytest.raises(SwarmError) as error:
            setup.launcher.complete(order, ack)
    assert str(error.value) == "hive launch acknowledgement does not match the live execution"


def test_live_process_with_an_expired_reservation_stays_ambiguous(setup):
    from scripts.swarm_v2.runtime.base import Outcome, Status

    _, order, ack = live_order(setup)
    setup.launcher.slots.release(order.execution_id)
    assert setup.launcher.complete(order, ack) == Outcome(
        "spawn", Status.AMBIGUOUS, "hive", detail="hive launch reservation expired"
    )
    assert setup.store.execution_registry.get("fixture", order.execution_id).state == "working"


def test_ambiguous_receipt_keeps_the_reservation_and_order(setup):
    from scripts.hive.orders import Ack
    from scripts.swarm_v2.runtime.base import Outcome, Status

    request = request_for(setup)
    order = order_for(setup, request, setup.launcher.select(setup.config, "eng", request.task))
    setup.store.redis.hset(setup.store.key("fixture", "controller-launch-orders"), "task", json.dumps(order.__dict__))
    setup.launcher.slots.reserve("claude:account", order.execution_id, 6, 300000)
    assert setup.launcher.complete(order, Ack("failed", {}, "ambiguous")) == Outcome(
        "spawn", Status.AMBIGUOUS, "hive", detail="hive launch failed: ambiguous"
    )
    assert setup.launcher.orders_for("task") == order
    assert setup.launcher.slots.counts() == {"claude:account": 1}
    assert setup.store.execution_registry.get("fixture", order.execution_id).state == "launching"


def test_same_claimant_replay_returns_the_same_live_execution(setup):
    request, order, _ = live_order(setup)
    placed = setup.launcher.spawn(request)
    assert placed.ok
    assert placed.value.execution.execution_id == order.execution_id
    assert len(setup.store.execution_registry.records("fixture")) == 1


def test_same_name_with_a_different_seat_cannot_replay_an_order(setup):
    from scripts.swarm_v2.runtime.base import Outcome, Status

    request, _, _ = live_order(setup)
    request = replace(request, task={**request.task, "seat": "eng-2@fixture"})
    assert setup.launcher.spawn(request) == Outcome(
        "spawn", Status.REFUSED, "hive", detail="another claimant owns the hive launch order"
    )


@pytest.mark.parametrize("state", ["ended", "finished"])
def test_terminal_execution_order_is_removed_before_later_admission(setup, state):
    request = request_for(setup)
    order = order_for(setup, request, setup.launcher.select(setup.config, "eng", request.task))
    agent = setup.store.execution_registry.get("fixture", order.execution_id)
    setup.store.execution_registry.update("fixture", replace(agent, state=state))
    setup.store.redis.hset(setup.store.key("fixture", "controller-launch-orders"), "task", json.dumps(order.__dict__))
    request = request_for(setup)
    setup.launcher.wait = Mock(return_value="pending")
    assert setup.launcher.spawn(request) == "pending"
    replacement = setup.launcher.orders_for("task")
    assert replacement.generation == order.generation + 1
    setup.launcher.wait.assert_called_once_with(replacement)


def test_legacy_launch_ending_allows_replacement_before_projection_catches_up(setup):
    request = request_for(setup)
    selected = setup.launcher.select(setup.config, "eng", request.task)
    previous = setup.launcher.admit(request, selected)
    setup.store.redis.hset(
        setup.store.key("fixture", "launch-endings"),
        previous.execution_id,
        json.dumps({"reason": "refused", "generation": previous.generation}),
    )
    next_request = request_for(setup)
    admitted = setup.launcher.admit(next_request, selected)
    assert admitted.generation == previous.generation + 1
    assert admitted.name == next_request.name
    assert admitted.profile_decision == selected.profile.record()


def test_unavailable_placement_does_not_admit_or_reserve(setup):
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm_v2.runtime.base import Outcome, Status

    setup.herdr._profile_decision.return_value = ProfileDecision("qa", "task", "qa")
    write_hive(setup, excluded=["qa"])
    request = request_for(setup)
    assert setup.launcher.spawn(request) == Outcome("spawn", Status.UNAVAILABLE, "hive", detail="no live hive takes qa")
    assert setup.store.execution_registry.records("fixture") == []
    assert setup.launcher.slots.counts() == {}


@pytest.mark.parametrize("harness", ["claude", "codex", "auto"])
def test_saved_assignment_precedes_the_lane_harness(setup, harness):
    from unittest.mock import patch

    from scripts.hive import launch
    from scripts.hive.placement import Wait

    saved = {"profile": "engineer", "harness": harness, "account": "account"}
    task = {"id": "task", "launch_assignment": saved}
    config = replace(setup.config, lanes={"eng": {"agent": "auto", "profile": "engineer"}})
    environ = {"fixture": "scope"}
    with patch.object(launch, "os", SimpleNamespace(environ=environ)):
        selected = setup.launcher.select(config, "eng", task)
    setup.herdr._profile_decision.assert_called_once_with(
        config, "eng", config.lanes["eng"], task, saved, saved, environ
    )
    if harness == "codex":
        assert selected == Wait("Codex hive placement is not enabled")
    else:
        assert selected.hive == "remote"


def test_handoff_assignment_is_forwarded_to_profile_resolution(setup):
    from unittest.mock import patch

    from scripts.hive import launch

    saved = {"profile": "engineer", "harness": "claude", "account": "account"}
    task = {"id": "task", "handoff_envelope": {"launch": saved}}
    with (
        patch.object(launch, "os", SimpleNamespace(environ={})),
        patch.object(launch.profile_choice, "transfer", return_value=saved) as transfer,
    ):
        assert setup.launcher.select(setup.config, "eng", task).hive == "remote"
    transfer.assert_called_once_with(task)
    setup.herdr._profile_decision.assert_called_once_with(setup.config, "eng", {}, task, saved, saved, {})


def test_initial_placement_reads_global_account_reports_without_a_credential_probe(setup):
    from unittest.mock import patch

    from scripts.hive import launch
    from scripts.swarm import lease

    rows = setup.herdr._quota_accounts
    del setup.herdr._quota_accounts
    now = lease.now_ms(setup.store)
    environ = {"fixture": "scope"}
    with (
        patch.object(launch, "os", SimpleNamespace(environ=environ)),
        patch.object(launch.lease, "now_ms", return_value=now),
        patch.object(launch.capacity, "accounts", return_value=rows) as accounts,
    ):
        assert setup.launcher.select(setup.config, "eng", {"id": "task"}).hive == "remote"
    accounts.assert_called_once_with(environ, now / 1000, refresh=False)


@pytest.mark.parametrize("pin", ["saved", "planned"])
def test_selected_account_is_frozen_ahead_of_less_used_accounts(setup, pin):
    from scripts.swarm.capacity import Account

    setup.herdr._quota_accounts = [
        Account("claude", "first", "room", 0, 80, 80, 6),
        Account("claude", "account", "room", 1, 80, 80, 6),
    ]
    write_hive(setup, slots=[{"harness": "claude", "account": "first", "kind": "subscription"}, *setup.hive["slots"]])
    task = {"id": "task"}
    if pin == "saved":
        task["launch_assignment"] = {"harness": "claude", "account": "account"}
    else:
        setup.herdr.planned_account.return_value = "account"
    assert setup.launcher.select(setup.config, "eng", task).account.name == "account"
    if pin == "planned":
        setup.herdr.planned_account.assert_called_once_with("task", "claude")


def test_account_selection_orders_room_by_usage_then_name(setup):
    from scripts.swarm.capacity import Account

    setup.herdr._quota_accounts = [
        Account("claude", "z", "room", 1, 80, 80, 6),
        Account("claude", "b", "room", 0, 80, 80, 6),
        Account("claude", "a", "room", 0, 80, 80, 6),
    ]
    write_hive(
        setup, slots=[{"harness": "claude", "account": name, "kind": "subscription"} for name in ("a", "b", "z")]
    )
    assert setup.launcher.select(setup.config, "eng", {"id": "task"}).account.name == "a"


@pytest.mark.parametrize("row", [("codex", 0, 6), ("claude", 6, 6)])
def test_non_claude_and_full_accounts_cannot_place_a_worker(setup, row):
    from scripts.hive.placement import Wait
    from scripts.swarm.capacity import Account

    harness, sessions, cap = row
    setup.herdr._quota_accounts = [Account(harness, "account", "room", sessions, 80, 80, cap)]
    assert setup.launcher.select(setup.config, "eng", {"id": "task"}) == Wait("no advertised Claude account has room")


@pytest.mark.parametrize(
    "ui,stale,expected",
    [("yes", False, "remote"), ("yes", True, "no live hive takes frontend"), ("no", False, "remote")],
)
def test_ui_profiles_take_any_live_hive_that_does_not_exclude_them(setup, ui, stale, expected):
    from scripts.hive import placement, registry
    from scripts.swarm import lease
    from scripts.swarm.profile_choice import ProfileDecision

    setup.herdr._profile_decision.return_value = ProfileDecision("frontend", "task", "frontend")
    write_hive(
        setup,
        ui=ui,
        excluded=[],
        heartbeat_at=lease.now_ms(setup.store) - 2 * registry.LIVE_MS if stale else lease.now_ms(setup.store),
    )
    selected = setup.launcher.select(setup.config, "eng", {"id": "task"})
    if expected == "remote":
        assert selected.hive == expected
    else:
        assert selected == placement.Wait(expected)


def test_interactive_placement_keeps_the_native_login_requirement(setup):
    from scripts.hive.placement import Wait

    assert setup.launcher.select(setup.config, "eng", {"id": "task", "route_kind": "interactive"}) == Wait(
        "no hive for eng holds the claude login for account"
    )
    write_hive(setup, interactive={"claude": "account", "codex": ""})
    assert (
        setup.launcher.select(setup.config, "eng", {"id": "task", "route_kind": "interactive"}).route_kind
        == "interactive"
    )


@pytest.mark.parametrize("state", ["launching", "working", "suspect"])
def test_unreported_executions_count_against_hive_room(setup, state):
    from scripts.hive.placement import Wait
    from scripts.swarm.store import AgentRecord

    names = [setup.store.next_name("fixture", "eng") for _ in range(3)]
    agents = [
        AgentRecord(
            name,
            "eng",
            "task",
            seat=f"eng-{i + 1}@fixture",
            state=state,
            hive="remote",
            runtime_target={"host_id": "remote"},
        )
        for i, name in enumerate(names)
    ]
    for agent in agents:
        setup.store.start_execution("fixture", agent)
    assert setup.launcher.select(setup.config, "eng", {"id": "next"}) == Wait("no hive for eng has room")


def test_foreign_ended_and_master_executions_do_not_use_worker_room(setup):
    from scripts.swarm.store import AgentRecord

    rows = [("eng", "other", "working"), ("eng", "remote", "ended"), ("master", "remote", "working")]
    for index, (lane, hive, state) in enumerate(rows):
        name = setup.store.next_name("fixture", lane)
        seat = "master@fixture" if lane == "master" else f"eng-{index + 1}@fixture"
        setup.store.start_execution(
            "fixture",
            AgentRecord(name, lane, "task", seat=seat, state=state, hive=hive, runtime_target={"host_id": hive}),
        )
    write_hive(setup, max_agents=1)
    assert setup.launcher.select(setup.config, "eng", {"id": "next"}).hive == "remote"


def test_heartbeat_execution_ids_prevent_double_counting_and_masters_are_exempt(setup):
    from scripts.hive import registry
    from scripts.swarm.store import AgentRecord

    name = setup.store.next_name("fixture", "eng")
    live = setup.store.start_execution(
        "fixture",
        AgentRecord(name, "eng", "task", seat="eng-1@fixture", hive="remote", runtime_target={"host_id": "remote"}),
    )
    setup.store.redis.set(
        f"{registry.key('remote')}:sessions",
        json.dumps(
            {
                "claude:account": 2,
                "interactive:claude:account": 1,
                "_executions": {"claude:account": [live.execution_id]},
            }
        ),
    )
    write_hive(setup, max_agents=2)
    assert setup.launcher.select(setup.config, "eng", {"id": "next"}).hive == "remote"
    assert setup.launcher.hives()[0]["agents"] == 1


@pytest.mark.parametrize(
    ("sessions", "listed", "expected"),
    [
        ({"claude:account": 3, "interactive:claude:account": 1}, 0, 3),
        ({"claude:account": 2}, 1, 3),
    ],
)
def test_a_hive_reporting_no_execution_ids_counts_each_running_agent_once(setup, sessions, listed, expected):
    from scripts.hive import registry
    from scripts.swarm.store import AgentRecord

    started = [
        setup.store.start_execution(
            "fixture",
            AgentRecord(
                setup.store.next_name("fixture", "eng"),
                "eng",
                "task",
                seat=f"eng-{index + 1}@fixture",
                state=state,
                hive="remote",
                runtime_target={"host_id": "remote"},
            ),
        )
        for index, state in enumerate(["working", "launching"] if not listed else ["working", "working"])
    ]
    if listed:
        sessions = {**sessions, "_executions": {"claude:account": [started[0].execution_id]}}
    setup.store.redis.set(f"{registry.key('remote')}:sessions", json.dumps(sessions))
    write_hive(setup, max_agents=expected + 1)
    assert setup.launcher.hives()[0]["agents"] == expected
    assert setup.launcher.select(setup.config, "eng", {"id": "next"}).hive == "remote"


def test_default_wait_sleep_and_exact_deadline_are_exercised(setup):
    from unittest.mock import patch

    from scripts.hive import launch
    from scripts.hive.orders import Ack
    from scripts.swarm import lease
    from scripts.swarm_v2.runtime.base import Outcome, Status

    request = request_for(setup)
    order = order_for(setup, request, setup.launcher.select(setup.config, "eng", request.task))
    now = lease.now_ms(setup.store)
    with patch.object(launch.time, "sleep") as sleep:
        launcher = launch.HiveLaunch(setup.store, "fixture", setup.held, setup.herdr)
        launcher.orders.ack = Mock(side_effect=[None, Ack("failed", {}, "prompt")])
        assert launcher.wait(replace(order, expires_at=now + 10000)) == Outcome(
            "spawn", Status.REFUSED, "hive", detail="hive launch failed: prompt"
        )
        sleep.assert_called_once_with(1)
    setup.launcher.sleep = Mock(side_effect=AssertionError("an expired wait must not sleep"))
    with patch.object(launch.lease, "now_ms", return_value=now):
        assert setup.launcher.wait(replace(order, expires_at=now)) == Outcome(
            "spawn", Status.AMBIGUOUS, "hive", detail="hive launch acknowledgement expired"
        )


def test_old_controller_cannot_admit_after_lease_takeover(setup):
    from scripts.swarm import lease
    from scripts.swarm.store import SwarmError

    request = request_for(setup)
    selected = setup.launcher.select(setup.config, "eng", request.task)
    assert lease.release(setup.store, "fixture", setup.held)
    held = lease.acquire(setup.store, "fixture", "next controller")
    assert held.epoch > setup.held.epoch
    with pytest.raises(SwarmError):
        setup.launcher.admit(request, selected)


def test_old_controller_cannot_record_an_acknowledgement_after_lease_takeover(setup):
    from scripts.swarm import lease
    from scripts.swarm.store import SwarmError

    _, order, ack = live_order(setup)
    admitted = setup.store.execution_registry.get("fixture", order.execution_id)
    assert lease.release(setup.store, "fixture", setup.held)
    lease.acquire(setup.store, "fixture", "next controller")
    with pytest.raises(SwarmError):
        setup.launcher.record(admitted, order, ack)
    assert setup.store.execution_registry.get("fixture", order.execution_id).state == "launching"


@pytest.mark.parametrize(
    "lane,profile", [("eng", "engineer"), ("ci", "cicd"), ("eng", "frontend"), ("dispatch", "dispatcher")]
)
def test_controller_spawn_uses_the_remote_backend_for_worker_profiles(setup, lane, profile):
    from scripts.hive.orders import Consumer
    from scripts.swarm.controller import FencedRuntime
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm.tick import Placed
    from scripts.swarm_v2.runtime.base import Outcome, RuntimeRouter, Status
    from scripts.swarm_v2.runtime.local import LocalHerdrRuntime
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    setup.herdr._profile_decision.return_value = ProfileDecision(profile, "task", profile)
    write_hive(setup, ui="yes", excluded=[])
    request = request_for(setup, lane=lane)
    request.task["profile_decision"] = setup.herdr._profile_decision.return_value.record()
    local = LocalHerdrRuntime(setup.herdr)
    original = RoutedRuntime(setup.herdr, RuntimeRouter([local]))
    fenced = FencedRuntime(setup.store, "fixture", setup.held, original, "compose")
    assert fenced.router.spawn_backend() == "hive"
    setup.herdr.quota_capacity.return_value = {"reason": "fixture capacity"}
    assert fenced.quota_capacity(setup.config, [], 12, refresh=False) == {"reason": "fixture capacity"}
    setup.herdr.quota_capacity.assert_called_once_with(setup.config, [], 12, refresh=False)
    worker = Mock()
    worker.spawn.return_value = Outcome(
        "spawn",
        Status.OK,
        "local",
        Placed(
            "pane",
            "claude",
            "account",
            model="selected-model",
            effort="high",
            profile=profile,
            profile_decision={"validation": {"pid": 12}},
        ),
    )
    worker.namespace.return_value = "namespace"
    worker.table.return_value = {12: SimpleNamespace(start_time=34)}
    consumer = Consumer(setup.store.redis, "remote", worker)

    def consume(_):
        assert consumer.poll() == 1

    fenced.runtime.hive_launch.sleep = consume
    assert fenced.placement_refusal(lane, request.task) == ""
    placed = fenced.spawn(setup.config, lane, request.name, request.task)
    assert placed.execution.runtime_target["host_id"] == "remote"
    assert placed.execution.name == request.name
    assert placed.execution.lane == lane
    assert placed.profile == profile
    assert placed.account == "account"
    assert placed.harness == "claude"
    assert placed.model == "selected-model"
    assert placed.effort == "high"
    setup.herdr.spawn.assert_not_called()


@pytest.mark.parametrize("disabled", [[], ["hive"], ["kubernetes"]])
def test_a_master_seat_owning_a_kubernetes_hive_follows_the_ui_host_rule_instead(setup, monkeypatch, disabled):
    from scripts.swarm import controller
    from scripts.swarm.controller import DISABLED, FencedRuntime, SwarmError
    from scripts.swarm.tick import Placed
    from scripts.swarm_v2.masters import MasterSeats
    from scripts.swarm_v2.runtime.base import Capability, Outcome, Placement, RuntimeRouter, Status
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    MasterSeats(setup.store.redis).set_count("fixture", 2)
    backend = Mock()
    backend.backend = "kubernetes"
    backend.capabilities = frozenset((Capability.SPAWN,))
    backend.spawn.return_value = Outcome("spawn", Status.OK, "kubernetes", Placed("master pane", "claude"))
    placement = Placement("kubernetes", master_seats=lambda: frozenset(("master-2",)))
    original = RoutedRuntime(setup.herdr, RuntimeRouter([backend], disabled=disabled, placement=placement))
    fenced = FencedRuntime(setup.store, "fixture", setup.held, original, "distributed")
    monkeypatch.setattr(controller, "_workstation_herdr", lambda: False)
    task = {"id": "master-2"}
    refused = (
        DISABLED.format(backend="hive")
        if disabled == ["hive"]
        else "the workstation hive spawns master work: no live hive takes master"
    )
    assert fenced.placement_refusal("master", task) == refused
    with pytest.raises(SwarmError, match=refused):
        fenced.spawn(setup.config, "master", "master name", task)
    backend.spawn.assert_not_called()
    monkeypatch.setattr(controller, "_workstation_herdr", lambda: True)
    assert fenced.placement_refusal("master", task) == ""


def test_controller_reports_a_placement_wait_for_a_worker_every_hive_excludes(setup):
    from scripts.swarm.controller import FencedRuntime
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    setup.herdr._profile_decision.return_value = ProfileDecision("qa", "task", "qa")
    write_hive(setup, excluded=["qa"])
    fenced = FencedRuntime(setup.store, "fixture", setup.held, RoutedRuntime(setup.herdr, RuntimeRouter([])), "compose")
    assert fenced.placement_refusal("eng", {"id": "task"}) == "no live hive takes qa"


def test_controller_ci_lane_cannot_replace_a_pinned_codex_harness_with_claude(setup):
    from scripts.swarm.controller import FencedRuntime
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    config = replace(setup.config, lanes={"ci": {"agent": "codex"}})
    setup.store.redis.hset(setup.store.key("fixture", "config"), "lanes", json.dumps(config.lanes))
    setup.herdr._profile_decision.return_value = ProfileDecision("cicd", "lane", "ci")
    fenced = FencedRuntime(setup.store, "fixture", setup.held, RoutedRuntime(setup.herdr, RuntimeRouter([])), "compose")
    assert fenced.placement_refusal("ci", {"id": "task"}) == "Codex hive placement is not enabled"


def test_spawn_honors_the_ci_lane_harness_before_admission(setup):
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm_v2.runtime.base import Outcome, Status

    request = request_for(setup, lane="ci")
    request = replace(request, config=replace(setup.config, lanes={"ci": {"agent": "codex"}}))
    setup.herdr._profile_decision.return_value = ProfileDecision("cicd", "lane", "ci")
    setup.launcher.sleep = Mock(side_effect=AssertionError("a rejected harness must not launch"))
    assert setup.launcher.spawn(request) == Outcome(
        "spawn", Status.UNAVAILABLE, "hive", detail="Codex hive placement is not enabled"
    )
    assert setup.store.execution_registry.records("fixture") == []


def test_reported_worker_sessions_fill_hive_room_without_execution_records(setup):
    from scripts.hive import registry
    from scripts.hive.placement import Wait

    setup.store.redis.set(f"{registry.key('remote')}:sessions", json.dumps({"claude:account": 3}))
    assert setup.launcher.select(setup.config, "eng", {"id": "task"}) == Wait("no hive for eng has room")


def test_terminal_order_is_cleared_even_when_replacement_has_no_account_room(setup):
    from scripts.swarm_v2.runtime.base import Outcome, Status

    request = request_for(setup)
    order = order_for(setup, request, setup.launcher.select(setup.config, "eng", request.task))
    previous = setup.store.execution_registry.get("fixture", order.execution_id)
    setup.store.execution_registry.update("fixture", replace(previous, state="finished"))
    setup.store.redis.hset(setup.store.key("fixture", "controller-launch-orders"), "task", json.dumps(order.__dict__))
    setup.herdr._quota_accounts = []
    assert setup.launcher.spawn(request_for(setup)) == Outcome(
        "spawn", Status.UNAVAILABLE, "hive", detail="no advertised Claude account has room"
    )
    assert setup.launcher.orders_for("task") is None


def test_placement_wait_records_original_start_and_clears_on_selection(setup):
    from dataclasses import replace

    from scripts.hive import placement_waits

    decision = setup.herdr._profile_decision.return_value
    setup.herdr._profile_decision.return_value = replace(decision, profile="frontend")
    write_hive(setup, excluded=["frontend"])
    setup.launcher.select(setup.config, "eng", {"id": "task"})
    first = placement_waits.read(setup.store, setup.config.slug)
    assert first["task"]["reason"] == "no live hive takes frontend"
    assert first["task"]["lane"] == "eng"
    assert first["task"]["started_at"] > 0
    setup.launcher.select(setup.config, "eng", {"id": "task"})
    assert placement_waits.read(setup.store, setup.config.slug) == first
    setup.herdr._profile_decision.return_value = decision
    setup.launcher.select(setup.config, "eng", {"id": "task"})
    assert placement_waits.read(setup.store, setup.config.slug) == {}


def test_missing_hive_finding_starts_at_thirty_minutes_and_is_shared(setup):
    from scripts.hive import placement, placement_waits

    for task in ("first", "second"):
        placement_waits.observe(setup.store, setup.config.slug, "eng", task, placement.NO_UI_HIVE, 1000)
    assert placement_waits.findings(setup.store, setup.config.slug, 1_800_999) == []
    found = placement_waits.findings(setup.store, setup.config.slug, 1_801_000)
    assert len(found) == 1
    assert found[0].kind == "missing hive"
    assert found[0].subject == "ui"
    assert found[0].summary == "Wanted ui hive has been missing for thirty minutes"
    assert found[0].threshold == "thirty minutes"
    assert found[0].measure == 1_800_000
    assert found == placement_waits.findings(setup.store, setup.config.slug, 1_801_000)
    placement_waits.observe(setup.store, setup.config.slug, "eng", "first", "", 1_801_000)
    assert len(placement_waits.findings(setup.store, setup.config.slug, 1_801_000)) == 1
    placement_waits.observe(setup.store, setup.config.slug, "eng", "second", "", 1_801_000)
    assert placement_waits.findings(setup.store, setup.config.slug, 1_801_000) == []


def test_capacity_wait_is_recorded_without_a_missing_hive_finding(setup):
    from scripts.hive import placement_waits

    setup.herdr._quota_accounts = []
    setup.launcher.select(setup.config, "eng", {"id": "task"})
    record = placement_waits.read(setup.store, setup.config.slug)["task"]
    assert record["reason"] == "no advertised Claude account has room"
    assert record["lane"] == "eng"
    assert placement_waits.findings(setup.store, setup.config.slug, record["started_at"] + 1_800_000) == []


def test_changed_wait_reason_restarts_the_missing_hive_clock(setup):
    from scripts.hive import placement, placement_waits

    placement_waits.observe(setup.store, setup.config.slug, "eng", "task", "no hive for eng has room", 1000)
    placement_waits.observe(setup.store, setup.config.slug, "eng", "task", placement.NO_UI_HIVE, 2_000_000)
    assert placement_waits.read(setup.store, setup.config.slug)["task"]["started_at"] == 2_000_000
    assert placement_waits.findings(setup.store, setup.config.slug, 2_000_001) == []


def test_missing_role_hives_raise_separate_findings_and_keep_the_oldest_wait(setup):
    from scripts.hive import placement_waits

    for lane, task, at in (
        ("eng", "first", 1000),
        ("eng", "second", 2000),
        ("ci", "third", 1000),
        ("eng", "equal", 1000),
    ):
        placement_waits.observe(setup.store, setup.config.slug, lane, task, f"no live hive takes {lane}", at)
    found = placement_waits.findings(setup.store, setup.config.slug, 1_802_000)
    assert [row.id for row in found] == ["missing-hive/ci", "missing-hive/eng"]
    assert found[1].measure == 1_801_000
    assert found[1].evidence == ("Task first waiting since 1000: no live hive takes eng",)


def test_swarm_health_reports_missing_hive_and_removes_completed_waits(setup, monkeypatch):
    from scripts.hive import placement, placement_waits
    from scripts.swarm import status

    placement_waits.observe(setup.store, setup.config.slug, "eng", "task", placement.NO_UI_HIVE, 1000)
    monkeypatch.setattr(status, "now_ms", lambda: 1_801_000)
    monkeypatch.setattr(status.activity, "counts", lambda slug: {})
    tasks = [{"id": "task", "state": "open"}]
    found = status.findings(setup.store, setup.config.slug, setup.config, tasks, [])
    assert [(row["kind"], row["subject"]) for row in found] == [("missing hive", "ui")]
    tasks[0]["state"] = "done"
    assert status.findings(setup.store, setup.config.slug, setup.config, tasks, []) == []
    assert placement_waits.read(setup.store, setup.config.slug) == {}


@pytest.mark.parametrize("state", ["done", "cancelled"])
def test_wait_pruning_keeps_active_tasks_and_drops_removed_tasks(setup, state):
    from scripts.hive import placement, placement_waits

    for task in ("active", "settled", "removed"):
        placement_waits.observe(setup.store, setup.config.slug, "eng", task, placement.NO_UI_HIVE, 1000)
    placement_waits.prune(setup.store, setup.config.slug, [{"id": "active"}, {"id": "settled", "state": state}])
    assert list(placement_waits.read(setup.store, setup.config.slug)) == ["active"]


def test_wait_record_retries_a_conflicting_writer(setup, monkeypatch):
    from redis.exceptions import WatchError

    from scripts.hive import placement, placement_waits

    pipeline = setup.store.redis.pipeline()
    execute = pipeline.execute
    attempts = []

    def conflict_once():
        attempts.append(True)
        if len(attempts) == 1:
            pipeline.reset()
            raise WatchError()
        return execute()

    monkeypatch.setattr(pipeline, "execute", conflict_once)
    monkeypatch.setattr(setup.store.redis, "pipeline", lambda: pipeline)
    placement_waits.observe(setup.store, setup.config.slug, "eng", "task", placement.NO_UI_HIVE, 1000)
    assert len(attempts) == 2
    assert placement_waits.read(setup.store, setup.config.slug)["task"] == {
        "lane": "eng",
        "reason": placement.NO_UI_HIVE,
        "started_at": 1000,
    }


def test_unrelated_and_recent_waits_do_not_hide_an_overdue_missing_hive(setup):
    from scripts.hive import placement, placement_waits

    placement_waits.observe(setup.store, setup.config.slug, "eng", "capacity", "no hive for eng has room", 1000)
    placement_waits.observe(setup.store, setup.config.slug, "ci", "recent", "no live hive takes ci", 2_000_000)
    placement_waits.observe(setup.store, setup.config.slug, "eng", "overdue", placement.NO_UI_HIVE, 1000)
    found = placement_waits.findings(setup.store, setup.config.slug, 2_000_001)
    assert [row.id for row in found] == ["missing-hive/ui"]
    assert found[0].evidence == ("Task overdue waiting since 1000: no UI hive live",)


def test_unsupported_harness_wait_records_its_lane(setup):
    from scripts.hive import placement_waits
    from scripts.hive.placement import Wait

    config = replace(setup.config, lanes={"ci": {"agent": "codex"}})
    assert setup.launcher.select(config, "ci", {"id": "task"}) == Wait("Codex hive placement is not enabled")
    assert placement_waits.read(setup.store, setup.config.slug)["task"]["lane"] == "ci"


def test_controller_records_the_execution_a_joined_hive_acknowledged(setup):
    from scripts.hive.orders import Ack
    from scripts.swarm import lease

    request = request_for(setup)
    selected = setup.launcher.select(setup.config, "eng", request.task)
    order = replace(order_for(setup, request, selected), expires_at=lease.now_ms(setup.store) + 60000)
    setup.store.redis.hset(setup.store.key("fixture", "controller-launch-orders"), "task", json.dumps(order.__dict__))
    setup.launcher.slots.reserve("claude:account", order.execution_id, 6, 300000)
    target = {"host_id": "remote", "process_namespace": "namespace", "pid": 12, "pid_start": 34}
    metadata = {"pane_id": "pane", "model": "selected-model", "effort": "high", "profile_decision": {"pid": 12}}
    setup.launcher.orders.finish("remote", "0-0", order, Ack("live", target, "", metadata))
    result = setup.launcher.wait(order)
    assert result.ok
    live = setup.store.execution_registry.get("fixture", order.execution_id)
    assert live.state == "working"
    assert live.runtime_target == target
    assert (live.pane_id, live.model, live.effort) == ("pane", "selected-model", "high")
    assert live.profile_decision == {"pid": 12}
    assert result.value.execution == live


def test_an_acknowledgement_without_metadata_keeps_the_admitted_fields(setup):
    from scripts.hive.orders import Ack

    _, order, ack = live_order(setup)
    admitted = setup.store.execution_registry.get("fixture", order.execution_id)
    assert setup.launcher.complete(order, Ack("live", ack.target)).ok
    live = setup.store.execution_registry.get("fixture", order.execution_id)
    assert live == replace(admitted, runtime_target=ack.target, state="working", profile_decision={})


def test_a_malformed_profile_decision_is_refused_before_recording(setup):
    from scripts.swarm.store import SwarmError

    _, order, ack = live_order(setup)
    with pytest.raises(SwarmError) as error:
        setup.launcher.complete(order, replace(ack, execution={**ack.execution, "profile_decision": []}))
    assert str(error.value) == "hive launch acknowledgement does not match the live execution"
    assert setup.store.execution_registry.get("fixture", order.execution_id).state == "launching"
