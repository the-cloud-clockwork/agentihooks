"""The hive join agent read stays bounded for a swarm that has no fencing document."""

from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _recording_redis(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[str]]:
    from fakeredis import FakeRedis

    from scripts.events import state

    monkeypatch.setenv(state.BACKEND_ENV, "redis")
    redis = FakeRedis(decode_responses=True)
    walked: list[str] = []
    scan, keys = redis.scan, redis.keys

    def recorded_scan(*args: Any, **kwargs: Any) -> Any:
        walked.append(f"scan {kwargs.get('match')}")
        return scan(*args, **kwargs)

    def recorded_keys(*args: Any, **kwargs: Any) -> Any:
        walked.append(f"keys {args}")
        return keys(*args, **kwargs)

    monkeypatch.setattr(redis, "scan", recorded_scan)
    monkeypatch.setattr(redis, "keys", recorded_keys)
    return redis, walked


@pytest.mark.feature("hive-join-fencing-read-bounded")
def test_the_hive_join_agent_read_for_a_swarm_with_no_fencing_entry_walks_no_keyspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.hive import join_report

    redis, walked = _recording_redis(monkeypatch)

    running = join_report.running_executions(redis, [{"slug": "proof-swarm"}])

    assert running == set()
    assert walked == []


@pytest.mark.feature("hive-join-fencing-read-bounded")
def test_a_swarm_that_predates_the_fencing_document_still_imports_its_legacy_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.swarm import fencing
    from scripts.swarm.store import RedisStore

    redis, walked = _recording_redis(monkeypatch)
    store = RedisStore(redis)
    redis.set(store.key("legacy", "control-epoch"), 3)
    redis.set(store.key("legacy", "task-authority", "t1"), '{"holder": "eng-a"}')
    redis.rpush(store.key("legacy", "claim-journal", "t1"), '{"event": "a"}')

    doc = fencing.read(store, "legacy")

    assert doc["claims"] == {"t1": {"holder": "eng-a"}}
    assert doc["journals"] == {"t1": [{"event": "a"}]}
    assert doc["epoch"] == 3
    assert walked
