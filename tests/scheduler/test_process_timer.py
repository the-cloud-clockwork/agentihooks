import json
from unittest.mock import Mock

import pytest

from scripts.scheduler import process_timer
from scripts.scheduler.core import Scheduler

pytestmark = pytest.mark.unit


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


def test_wait_is_released_by_the_scheduler_signal(redis, monkeypatch):
    monkeypatch.setattr(process_timer.service, "ensure_running", Mock())
    with process_timer.Timer(2, redis=redis) as timer:
        entry = timer.entry
        assert entry.record["delay"] == 2
        assert entry.record["state"] == "active"
        assert entry.record["slots"][0]["reference"] == "scripts.scheduler.process_timer:wake"
        assert redis.llen(timer.key) == 0
        scheduler = Scheduler(redis)
        assert scheduler.run_due(now=entry.record["due"] - 1) == 0
        assert redis.llen(timer.key) == 0
        assert scheduler.run_due(now=entry.record["due"]) == 1
        timer.wait()
        assert redis.llen(timer.key) == 0
    assert entry.state == "cancelled"
    assert not redis.exists(timer.key)
    process_timer.service.ensure_running.assert_called_once_with()


def test_interruption_cancels_the_timer_and_cleans_the_wake(redis, monkeypatch):
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    with pytest.raises(KeyboardInterrupt), process_timer.Timer(3, redis=redis) as timer:
        raise KeyboardInterrupt
    assert timer.entry.state == "cancelled"
    assert not redis.exists(timer.key)
    assert Scheduler(redis).run_due(now=timer.entry.record["due"]) == 0


def test_wait_uses_the_configured_redis_client(redis, monkeypatch):
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    monkeypatch.setattr(process_timer, "redis_client", lambda: redis)
    with process_timer.Timer(0) as timer:
        Scheduler(redis).run_due(now=timer.entry.record["due"])
        timer.wait()
    assert not redis.exists(timer.key)


def test_duplicate_delivery_keeps_one_wake(redis, monkeypatch):
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    with process_timer.Timer(3, redis=redis) as timer:
        event = timer.entry.record
        process_timer.wake(event, "same", redis)
        process_timer.wake(event, "same", redis)
        assert redis.lrange(timer.key, 0, -1) == ["same"]
        assert redis.ttl(timer.key) > 0
        assert json.loads(event["payload"])["key"] == timer.key
