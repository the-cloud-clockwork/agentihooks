"""A restarted hive controller reads account quota from the hive reports in Redis, never a pod local cache."""

import json
import time
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
    monkeypatch.setattr("scripts.routing.reservation._store", lambda environ: None)
    monkeypatch.setattr("scripts.routing.reservation.current", lambda harness, environ: None)


def _report(redis: Any, hive: str, sessions: int, used: int, now: float = NOW) -> None:
    registry.update(redis, hive, ["max-agents=8"])
    registry.beat(
        redis,
        hive,
        {
            "heartbeat_at": int(now * 1000),
            "slots": [{"harness": "claude", "account": "one", "kind": "subscription"}],
        },
    )
    redis.set(f"{registry.key(hive)}:sessions", json.dumps({"claude:one": sessions}), ex=45)
    quota = {
        "claude:one": {
            "five_hour": {"used": used, "resets_at": now + 3600},
            "seven_day": {"used": 10, "resets_at": now + 86400},
            "observed_at": now - 5,
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
    assert held is not None
    placed: RoutedRuntime = hive_controller(routed({}), store, config.slug, held)
    reader = placed.herdr_runtime.quota_accounts
    assert reader is not None
    (row,) = [row for row in reader({}, NOW, refresh=False) if row.harness == "claude"]
    assert (row.name, row.sessions) == ("one", 2)
    assert row.cap is not None


@pytest.mark.feature("controller-quota-after-restart")
def test_the_rotation_reads_the_hive_reports_before_the_pass_has_quota_rows(redis: Any) -> None:
    from unittest.mock import Mock

    from scripts.swarm.runtime import HerdrRuntime

    _report(redis, "remote", 1, 20, time.time())
    herdr = HerdrRuntime(choose=Mock(side_effect=AssertionError("the rotation must not read the pod cache")))
    herdr.quota_accounts = capacity.hive_accounts(redis)
    assert herdr._rotation("", {}) == ("claude", "rotation")
