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
