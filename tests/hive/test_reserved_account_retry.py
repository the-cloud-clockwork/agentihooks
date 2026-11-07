"""A hive spawn refused by a full account reservation moves to the next account with room in the same pass."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

FIRST_FULL = "account reservation is full: account=claude:first sessions=1 cap=1 five_left=80 week_left=80"
SECOND_FULL = "account reservation is full: account=claude:second sessions=6 cap=6 five_left=80 week_left=80"


@pytest.fixture
def setup() -> SimpleNamespace:
    import fakeredis

    from scripts.hive import admission, headroom, launch, registry
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
        slots=[
            {"harness": "claude", "account": "first", "kind": "subscription"},
            {"harness": "claude", "account": "second", "kind": "subscription"},
        ],
        repos=[config.repo],
        heartbeat_at=lease.now_ms(store),
        max_agents=3,
    )
    store.redis.hset(registry.key("remote"), mapping={k: registry._encode(k, v) for k, v in hive.items()})
    store.redis.sadd(registry.INDEX, "remote")
    reading = {
        "cpus": 1000,
        "load5": 0.0,
        "available_mb": 20_000 + admission.AGENT_MB * 3,
        "total_mb": 100_000,
        "pending": 0,
        "measured_at": lease.now_ms(store),
    }
    store.redis.set(headroom.key("remote"), json.dumps(reading))
    herdr = Mock(spec=HerdrRuntime)
    herdr.pass_memo = None
    herdr._profile_decision.return_value = ProfileDecision("engineer", "task", "engineer")
    herdr._quota_accounts = [
        Account("claude", "first", "room", 0, 80, 80, 1),
        Account("claude", "second", "room", 1, 80, 80, 6),
    ]
    herdr.planned_account.return_value = None
    launcher = launch.HiveLaunch(store, config.slug, held, herdr)
    launcher.opening = lambda request: f"opening for {request.name}"
    launcher.wait = Mock(return_value="pending")
    return SimpleNamespace(store=store, config=config, held=held, launcher=launcher)


def request_for(setup: SimpleNamespace) -> object:
    from scripts.swarm.store import AgentRecord
    from scripts.swarm_v2.runtime.base import SpawnRequest

    name = setup.store.next_name("fixture", "eng")
    record = AgentRecord(name, "eng", "task", seat="eng-1@fixture", state="starting")
    setup.store.put_agent("fixture", record)
    setup.store.seats.occupy(record.seat, name, 0)
    task = {"id": "task", "seat": record.seat, "controller_epoch": setup.held.epoch, "route_kind": "subscription"}
    return SpawnRequest(setup.config, "eng", name, task)


def test_a_full_reservation_launches_on_the_next_account_with_room(setup: SimpleNamespace) -> None:
    from scripts.hive import admission

    assert setup.launcher.slots.reserve("claude:first", "master", 1, 300000)
    request = request_for(setup)
    assert setup.launcher.spawn(request) == "pending"
    order = setup.launcher.orders_for("task")
    assert order.slot == "second"
    assert order.harness == "claude"
    assert len(setup.store.redis.xrange(setup.launcher.orders.stream("remote"))) == 1
    records = sorted(setup.store.execution_registry.records("fixture"), key=lambda record: record.generation)
    assert [(record.account, record.state) for record in records] == [("first", "ended"), ("second", "launching")]
    ending = setup.store.redis.hget(setup.store.key("fixture", "launch-endings"), records[0].execution_id)
    assert json.loads(ending)["reason"] == FIRST_FULL
    assert setup.store.redis.zrange(admission.key("remote"), 0, -1) == [f"fixture/{request.name}"]


def test_every_account_full_keeps_the_last_refusal(setup: SimpleNamespace) -> None:
    from scripts.hive import admission
    from scripts.swarm_v2.runtime.base import Status

    assert setup.launcher.slots.reserve("claude:first", "master", 1, 300000)
    for index in range(6):
        assert setup.launcher.slots.reserve("claude:second", f"other-{index}", 6, 300000)
    result = setup.launcher.spawn(request_for(setup))
    assert result.status is Status.UNAVAILABLE
    assert result.detail == SECOND_FULL
    assert result.value == {"harness": "claude"}
    assert setup.launcher.orders_for("task") is None
    assert setup.store.redis.xrange(setup.launcher.orders.stream("remote")) == []
    assert [record.state for record in setup.store.execution_registry.records("fixture")] == ["ended", "ended"]
    assert setup.store.redis.zrange(admission.key("remote"), 0, -1) == []


def test_a_retry_within_one_pass_skips_the_refused_account(setup: SimpleNamespace) -> None:
    assert setup.launcher.slots.reserve("claude:first", "master", 1, 300000)
    setup.launcher.placement_cache = {}
    request = request_for(setup)
    assert setup.launcher.spawn(request) == "pending"
    assert setup.launcher.placement_cache[("eng", "task")].account.name == "second"
