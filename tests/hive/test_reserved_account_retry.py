"""A hive spawn refused by a full account reservation moves to the next account with room in the same pass."""

import json
from types import SimpleNamespace
from typing import Any
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
    assert held is not None
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


def request_for(setup: SimpleNamespace, config: Any = None) -> Any:
    from scripts.swarm.store import AgentRecord
    from scripts.swarm_v2.runtime.base import SpawnRequest

    name = setup.store.next_name("fixture", "eng")
    record = AgentRecord(name, "eng", "task", seat="eng-1@fixture", state="starting")
    setup.store.put_agent("fixture", record)
    setup.store.seats.occupy(record.seat, name, 0)
    task = {"id": "task", "seat": record.seat, "controller_epoch": setup.held.epoch, "route_kind": "subscription"}
    return SpawnRequest(config or setup.config, "eng", name, task)


def add_hive(setup: SimpleNamespace, hive_id: str, slots: list[tuple[str, str]]) -> None:
    from scripts.hive import headroom, registry
    from scripts.swarm import lease

    hive = registry._default(hive_id)
    hive.update(
        slots=[{"harness": harness, "account": account, "kind": "subscription"} for harness, account in slots],
        repos=[setup.config.repo],
        heartbeat_at=lease.now_ms(setup.store),
        max_agents=3,
    )
    setup.store.redis.hset(registry.key(hive_id), mapping={k: registry._encode(k, v) for k, v in hive.items()})
    setup.store.redis.sadd(registry.INDEX, hive_id)
    setup.store.redis.set(headroom.key(hive_id), setup.store.redis.get(headroom.key("remote")))


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
    room = setup.launcher.hives()[0]["room"]
    request = request_for(setup)
    assert setup.launcher.spawn(request) == "pending"
    assert setup.launcher.placement_cache[("eng", "task")].account.name == "second"
    assert setup.launcher.hives()[0]["room"] == room - 1


def test_a_retry_on_another_hive_spends_that_hive_room(setup: SimpleNamespace) -> None:
    from scripts.hive import admission, headroom, registry
    from scripts.swarm import lease

    for hive_id, account in (("remote", "first"), ("other", "second")):
        hive = registry._default(hive_id)
        hive.update(
            slots=[{"harness": "claude", "account": account, "kind": "subscription"}],
            repos=[setup.config.repo],
            heartbeat_at=lease.now_ms(setup.store),
            max_agents=3,
        )
        setup.store.redis.hset(registry.key(hive_id), mapping={k: registry._encode(k, v) for k, v in hive.items()})
        setup.store.redis.sadd(registry.INDEX, hive_id)
        setup.store.redis.set(headroom.key(hive_id), setup.store.redis.get(headroom.key("remote")))
    assert setup.launcher.slots.reserve("claude:first", "master", 1, 300000)
    setup.launcher.placement_cache = {}
    before = {hive["id"]: hive["room"] for hive in setup.launcher.hives()}
    request = request_for(setup)
    assert setup.launcher.spawn(request) == "pending"
    assert setup.launcher.orders_for("task").hive == "other"
    after = {hive["id"]: hive["room"] for hive in setup.launcher.hives()}
    assert after == {"remote": before["remote"] - 1, "other": before["other"] - 1}
    assert setup.store.redis.zrange(admission.key("remote"), 0, -1) == []
    assert setup.store.redis.zrange(admission.key("other"), 0, -1) == [f"fixture/{request.name}"]


def test_a_hive_refusal_during_a_retry_keeps_the_refused_account_skipped(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hive import registry

    setup.store.redis.srem(registry.INDEX, "remote")
    add_hive(setup, "alpha", [("claude", "second")])
    add_hive(setup, "beta", [("claude", "first"), ("claude", "second")])
    assert setup.launcher.slots.reserve("claude:first", "master", 1, 300000)
    original = setup.launcher.reserve_room
    refused: list[tuple[str, str]] = []

    def reserve_room(request: Any, selected: Any) -> str:
        if selected.hive == "alpha" and not refused:
            refused.append((selected.hive, selected.account.name))
            return "no hive for eng has room: alpha is full"
        return original(request, selected)

    monkeypatch.setattr(setup.launcher, "reserve_room", reserve_room)
    assert setup.launcher.spawn(request_for(setup)) == "pending"
    order = setup.launcher.orders_for("task")
    assert (order.hive, order.slot) == ("beta", "second")
    assert refused == [("alpha", "second")]


def test_a_retry_keeps_the_harness_its_lane_names(setup: SimpleNamespace) -> None:
    from dataclasses import replace

    from scripts.hive import registry
    from scripts.swarm.capacity import Account

    setup.store.redis.srem(registry.INDEX, "remote")
    add_hive(setup, "codex-hive", [("codex", "first"), ("codex", "second")])
    setup.herdr._quota_tasks = {}
    setup.herdr._quota_accounts = [
        Account("codex", "first", "room", 0, 80, 80, 1),
        Account("codex", "second", "room", 1, 80, 80, 6),
    ]
    assert setup.launcher.slots.reserve("codex:first", "master", 1, 300000)
    config = replace(setup.config, lanes={"eng": {"agent": "codex"}})
    assert setup.launcher.spawn(request_for(setup, config)) == "pending"
    order = setup.launcher.orders_for("task")
    assert (order.harness, order.slot) == ("codex", "second")


def test_only_an_unavailable_full_reservation_counts_as_a_full_account() -> None:
    from scripts.hive import launch
    from scripts.swarm_v2.runtime.base import Outcome, Status

    assert launch._account_full(Outcome("spawn", Status.UNAVAILABLE, "hive", detail=FIRST_FULL))
    assert not launch._account_full(Outcome("spawn", Status.REFUSED, "hive", detail=FIRST_FULL))
    assert not launch._account_full(Outcome("spawn", Status.UNAVAILABLE, "hive", detail="no hive has room"))
    assert not launch._account_full(Outcome("spawn", Status.UNAVAILABLE, "hive", detail=f"queued {FIRST_FULL}"))
    assert not launch._account_full("pending")
