import json
from unittest.mock import Mock, call

import pytest

from scripts.scheduler import process_timer
from scripts.scheduler.core import Scheduler

pytestmark = pytest.mark.unit


@pytest.fixture
def redis(monkeypatch):
    import fakeredis

    connection = fakeredis.FakeRedis(decode_responses=True)
    pop = connection.blpop

    def bounded_pop(key, timeout):
        assert timeout == process_timer.RECHECK_S
        result = pop(key, timeout=0.2)
        if result is None:
            raise TimeoutError("scheduler delivered no wake")
        return result

    monkeypatch.setattr(connection, "blpop", bounded_pop)
    return connection


def test_wait_is_released_by_the_scheduler_signal(redis, monkeypatch):
    monkeypatch.setattr(process_timer.service, "ensure_running", Mock())
    close = Mock()
    monkeypatch.setattr(redis, "close", close)
    with process_timer.Timer(2, redis=redis) as timer:
        entry = timer.entry
        assert entry.record["delay"] == 2
        assert entry.record["owner"] == "process"
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
    close.assert_not_called()


def test_a_wait_with_no_wake_by_its_recheck_starts_the_scheduler_again(monkeypatch):
    ensure = Mock()
    monkeypatch.setattr(process_timer.service, "ensure_running", ensure)
    timer = process_timer.Timer.__new__(process_timer.Timer)
    timer.key = "wake"
    timer.redis = Mock()
    timer.redis.blpop.side_effect = [None, None, ("wake", "1")]
    timer.wait()
    assert timer.redis.blpop.call_args_list == [call("wake", timeout=process_timer.RECHECK_S)] * 3
    assert ensure.call_args_list == [call(), call()]


def test_interruption_cancels_the_timer_and_cleans_the_wake(redis, monkeypatch):
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    with pytest.raises(KeyboardInterrupt), process_timer.Timer(3, redis=redis) as timer:
        raise KeyboardInterrupt
    assert timer.entry.state == "cancelled"
    assert not redis.exists(timer.key)
    assert Scheduler(redis).run_due(now=timer.entry.record["due"]) == 0


def test_wait_uses_the_configured_redis_client(redis, monkeypatch):
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    monkeypatch.setattr(process_timer, "blocking_client", lambda: redis)
    close = Mock()
    monkeypatch.setattr(redis, "close", close)
    with process_timer.Timer(0) as timer:
        Scheduler(redis).run_due(now=timer.entry.record["due"])
        timer.wait()
    assert not redis.exists(timer.key)
    close.assert_called_once_with()


def test_duplicate_delivery_keeps_one_wake(redis, monkeypatch):
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    with process_timer.Timer(3, redis=redis) as timer:
        event = timer.entry.record
        redis.rpush(timer.key, "pending", "cancelled")
        process_timer.wake(event, "first", redis)
        assert redis.lrange(timer.key, 0, -1) == ["first"]
        process_timer.wake(event, "same", redis)
        process_timer.wake(event, "same", redis)
        assert redis.lrange(timer.key, 0, -1) == ["same"]
        assert redis.ttl(timer.key) == -1
        assert json.loads(event["payload"])["key"] == timer.key
    assert not redis.exists(timer.key)


def test_blocking_client_keeps_connection_settings_without_a_read_timeout(monkeypatch):
    import redis

    source = Mock()
    source.connection_pool.connection_class = object
    source.connection_pool.connection_kwargs = {"host": "scheduler", "socket_timeout": 10, "decode_responses": True}
    pool, client = Mock(), Mock()
    constructor = Mock(return_value=pool)
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda: source)
    monkeypatch.setattr(redis, "ConnectionPool", constructor)
    monkeypatch.setattr(redis, "Redis", Mock(return_value=client))
    assert process_timer.blocking_client() is client
    constructor.assert_called_once_with(
        connection_class=object, host="scheduler", socket_timeout=None, decode_responses=True
    )
    source.close.assert_called_once_with()


async def test_async_wait_cancels_its_timer_and_unblocks_its_worker(redis, monkeypatch):
    import asyncio

    started = asyncio.Event()
    timers, popped = [], []
    original = process_timer.Timer
    pop = redis.blpop

    def bounded_pop(key, timeout):
        result = pop(key, timeout=timeout)
        popped.append(result)
        return result

    def timer(delay):
        result = original(delay, redis=redis)
        timers.append(result)
        started.set()
        return result

    monkeypatch.setattr(redis, "blpop", bounded_pop)
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    monkeypatch.setattr(process_timer, "Timer", timer)
    task = asyncio.create_task(process_timer.async_wait(60))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(popped) == 1
    assert popped[0] is not None
    assert popped[0][0] == timers[0].key
    assert timers[0].entry.state == "cancelled"
    assert not redis.exists(timers[0].key)


@pytest.mark.parametrize("deployment", ["cloud", "local"])
def test_member_heartbeat_timer_uses_its_writable_subtree(redis, monkeypatch, deployment):
    from fnmatch import fnmatchcase

    from scripts.hive.auth import _acl_keys
    from scripts.swarm.keyspace import ROOT

    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", deployment)
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "member")
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    with process_timer.Timer(15, redis=redis) as timer:
        expected = (
            f"{ROOT}:hive:member:scheduler:"
            if deployment != "local"
            else process_timer.Scheduler(redis).key("wake:").rsplit("wake:", 1)[0]
        )
        assert timer.key.startswith(expected)
        if deployment != "local":
            writable = [pattern[1:] for pattern in _acl_keys("member") if pattern.startswith("~")]
            assert all(any(fnmatchcase(key, pattern) for pattern in writable) for key in redis.keys("*"))
        Scheduler(redis).run_due(now=timer.entry.record["due"])
        timer.wait()


def test_member_grants_still_refuse_shared_scheduler_writes(redis, monkeypatch):
    from fnmatch import fnmatchcase

    from scripts.hive.auth import _acl_keys

    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    shared = Scheduler(redis).key("entries")
    writable = [pattern[1:] for pattern in _acl_keys("member") if pattern.startswith("~")]
    assert not any(fnmatchcase(shared, pattern) for pattern in writable)


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_wait_helpers_consume_and_close_a_fired_timer(redis, monkeypatch, asynchronous):
    timers = []
    original = process_timer.Timer

    def timer(delay):
        result = original(delay, redis=redis)
        timers.append(result)
        Scheduler(redis).run_due(now=result.entry.record["due"])
        return result

    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    monkeypatch.setattr(process_timer, "Timer", timer)
    if asynchronous:
        await process_timer.async_wait(2)
    else:
        process_timer.wait(2)
    assert len(timers) == 1
    assert timers[0].entry.record["delay"] == 2
    assert timers[0].entry.state == "cancelled"
    assert not redis.exists(timers[0].key)


def test_controller_namespace_changes_only_in_hosted_mode(redis, monkeypatch):
    from hooks._redis import redis_key
    from scripts.swarm.keyspace import ROOT

    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "cloud")
    monkeypatch.delenv("AGENTIHOOKS_HIVE_ID", raising=False)
    assert Scheduler(redis, machine="worker").key("entries") == f"{ROOT}:scheduler:worker:entries"
    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT")
    assert Scheduler(redis, machine="worker").key("entries") == redis_key("scheduler", "worker:entries")


def test_member_identity_does_not_change_host_keys_in_local_or_default_mode(redis, monkeypatch):
    from hooks._redis import redis_key

    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "member")
    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT", raising=False)
    assert Scheduler(redis, machine="worker").key("entries") == redis_key("scheduler", "worker:entries")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    assert Scheduler(redis, machine="worker").key("entries") == redis_key("scheduler", "worker:entries")


def test_simultaneous_process_timers_have_independent_wake_queues(redis, monkeypatch):
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    with process_timer.Timer(0, redis=redis) as first, process_timer.Timer(0, redis=redis) as second:
        assert first.key != second.key
        assert first.entry.id != second.entry.id
        scheduler = Scheduler(redis)
        scheduler.run_due(now=max(first.entry.record["due"], second.entry.record["due"]))
        first.wait()
        assert redis.llen(second.key) == 1
        second.wait()


def test_process_timer_records_are_bounded_across_repeated_waits(redis, monkeypatch):
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    scheduler = Scheduler(redis)
    for _ in range(20):
        with process_timer.Timer(0, redis=redis) as timer:
            scheduler.run_due(now=timer.entry.record["due"])
            timer.wait()
        scheduler.run_due()
        assert redis.hlen(scheduler.key("entries")) == 0
        assert not redis.exists(timer.key)
        assert not redis.exists(scheduler.key("retired"))


def test_a_scheduler_that_cannot_start_leaves_no_timer_behind(redis, monkeypatch):
    def unwritable():
        raise PermissionError("cache folder")

    monkeypatch.setattr(process_timer.service, "ensure_running", unwritable)
    scheduler = Scheduler(redis)
    with pytest.raises(PermissionError):
        process_timer.Timer(5, redis=redis)
    assert len(redis.hkeys(scheduler.key("retired"))) == 1
    scheduler.run_due()
    assert redis.hlen(scheduler.key("entries")) == 0
    assert not redis.keys(scheduler.key("wake:*"))


def test_delivered_wake_survives_a_paused_consumer(redis, monkeypatch):
    import time

    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    with process_timer.Timer(0, redis=redis) as timer:
        Scheduler(redis).run_due(now=timer.entry.record["due"])
        now = time.time()
        monkeypatch.setattr(time, "time", lambda: now + 120)
        assert redis.ttl(timer.key) == -1
        timer.wait()


def test_retirement_during_dispatch_removes_a_late_wake(redis, monkeypatch):
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    original = process_timer.wake
    with process_timer.Timer(0, redis=redis) as timer:

        def retire_then_wake(event, delivery_id, connection):
            timer.close()
            original(event, delivery_id, connection)

        monkeypatch.setattr(process_timer, "wake", retire_then_wake)
        Scheduler(redis).run_due(now=timer.entry.record["due"])
    assert redis.hlen(Scheduler(redis).key("entries")) == 0
    assert not redis.exists(timer.key)


def test_member_schedules_and_consumes_wakes_while_shared_writes_are_refused(monkeypatch):
    import fakeredis
    from redis.exceptions import NoPermissionError

    from scripts.hive import auth

    server = fakeredis.FakeServer()
    admin = fakeredis.FakeRedis(server=server, decode_responses=True)
    admin.acl_setuser(
        "hive-member",
        enabled=True,
        nopass=True,
        categories=auth.ACL_CATEGORIES,
        keys=auth._acl_keys("member"),
        channels=auth.ACL_PATTERNS,
    )
    member = fakeredis.FakeRedis(server=server, decode_responses=True, username="hive-member")
    member.auth("", username="hive-member")
    assert member.acl_whoami() == "hive-member"
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    shared = Scheduler(admin).key("entries")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "cloud")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "member")
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    with process_timer.Timer(0, redis=member) as timer:
        assert Scheduler(admin).run_due(now=timer.entry.record["due"]) == 1
        assert admin.llen(timer.key) == 1
        timer.wait()
    Scheduler(admin).run_due()
    assert not admin.exists(timer.key)
    with pytest.raises(NoPermissionError):
        member.hset(shared, "forbidden", "write")
