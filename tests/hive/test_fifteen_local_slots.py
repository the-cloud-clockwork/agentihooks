from dataclasses import replace

import fakeredis
import pytest

from scripts.hive import registry
from scripts.swarm import commands, host_budget, local_slots
from scripts.swarm.store import RedisStore, SwarmConfig


@pytest.fixture
def store() -> RedisStore:
    result = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    result.create(SwarmConfig("workstation", "/repo", 15, 0, max_plan=0))
    return result


def test_new_and_legacy_swarms_allow_fifteen_local_workers(store: RedisStore) -> None:
    assert store.config("workstation").local_max_agents == 15
    store.redis.hdel(store.key("workstation", "config"), "local_max_agents")
    assert store.config("workstation").local_max_agents == 15
    store.redis.hset(store.key("workstation", "config"), "local_max_agents", "8")
    assert store.config("workstation").local_max_agents == 8


def test_headed_hive_and_swarm_defaults_allow_fifteen_slots(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    hive = registry.update(store.redis, "workstation", ["ui=yes"])
    assert hive["max_agents"] == 15
    assert local_slots.maximum(store, "workstation", 50) == 15
    assert registry.update(store.redis, "remote", ["ui=no"])["max_agents"] == 20


@pytest.mark.parametrize(("local", "hive", "ceiling", "expected"), [(7, 15, 50, 7), (15, 6, 50, 6), (15, 15, 4, 4)])
def test_explicit_slot_limits_still_bound_fifteen_workers(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, local: int, hive: int, ceiling: int, expected: int
) -> None:
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    registry.update(store.redis, "workstation", ["ui=yes", f"max-agents={hive}"])
    store.redis.hset(store.key("workstation", "config"), "local_max_agents", str(local))
    assert local_slots.maximum(store, "workstation", ceiling) == expected


@pytest.mark.parametrize(
    "sample",
    [host_budget.HostSample(32, 8, 64_000, 15), host_budget.HostSample(0.5, 8, 100, 15)],
)
def test_fifteen_worker_configuration_keeps_host_room_guard(store: RedisStore, sample: host_budget.HostSample) -> None:
    config = replace(store.config("workstation"), local_max_agents=15)
    assert host_budget.spawn_room(config, sample, 15).room == 0
