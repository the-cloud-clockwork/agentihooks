"""A restarted hive controller reads account quota from the hive reports in Redis, never a pod local cache."""

import json
from typing import Any

import pytest

from scripts import claude_quota_balancer as balancer
from scripts.hive import registry
from scripts.swarm import capacity

NOW = 1000.0


@pytest.fixture
def redis() -> Any:
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture(autouse=True)
def empty_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(balancer, "cached_observations", lambda **kwargs: [])
    monkeypatch.setattr(capacity.account_sessions, "codex_sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda **kwargs: {})
    monkeypatch.setattr("hooks._redis.get_redis", lambda: None)


def _report(redis: Any, hive: str, sessions: int, used: int) -> None:
    registry.update(redis, hive, ["max-agents=8"])
    registry.beat(
        redis,
        hive,
        {
            "heartbeat_at": int(NOW * 1000),
            "slots": [{"harness": "claude", "account": "one", "kind": "subscription"}],
        },
    )
    redis.set(f"{registry.key(hive)}:sessions", json.dumps({"claude:one": sessions}), ex=45)
    quota = {
        "claude:one": {
            "five_hour": {"used": used, "resets_at": NOW + 3600},
            "seven_day": {"used": 10, "resets_at": NOW + 86400},
            "observed_at": NOW - 5,
        }
    }
    redis.set(f"{registry.key(hive)}:quota", json.dumps(quota), ex=45)


@pytest.mark.feature("controller-quota-after-restart")
def test_a_restarted_controller_reads_a_fresh_cap_from_the_hive_quota_reports(redis: Any) -> None:
    _report(redis, "remote", 5, 20)
    rows = capacity.hive_accounts(redis)({}, NOW, refresh=False)
    (row,) = [row for row in rows if row.harness == "claude"]
    assert (row.name, row.sessions, row.observed_at) == ("one", 5, NOW - 5)
    assert row.cap is not None and row.cap > row.sessions
    assert row.state != "UNKNOWN"


@pytest.mark.feature("controller-quota-after-restart")
def test_the_hive_controller_runtime_reads_quota_from_its_store(redis: Any) -> None:
    from scripts.swarm import lease
    from scripts.swarm.store import RedisStore, SwarmConfig
    from scripts.swarm_v2.runtime.routed import RoutedRuntime, hive_controller, routed

    store = RedisStore(redis)
    config = SwarmConfig("fixture", "agentihooks", 1, 1)
    store.create(config)
    _report(redis, "remote", 2, 20)
    held = lease.acquire(store, config.slug, "controller")
    placed: RoutedRuntime = hive_controller(routed({}), store, config.slug, held)
    reader = placed.herdr_runtime.quota_accounts
    assert reader is not None
    (row,) = [row for row in reader({}, NOW, refresh=False) if row.harness == "claude"]
    assert (row.name, row.sessions) == ("one", 2)
    assert row.cap is not None


@pytest.mark.feature("controller-quota-after-restart")
def test_a_missing_quota_reading_names_its_reason_in_the_placement_wait() -> None:
    from scripts.hive.launch import seat_text

    unread = capacity.Account("claude", "one", "UNKNOWN", 5, None, None)
    read = capacity.Account("claude", "two", "FULL", 6, 40, 40, 6)
    assert seat_text(unread) == "one 5 sessions, no fresh quota reading"
    assert seat_text(read) == "two 6 sessions of 6"
