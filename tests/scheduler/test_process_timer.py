import json
import time
from unittest.mock import Mock

import pytest

from scripts.events.redis_state import RedisStateStore
from scripts.scheduler import process_timer
from scripts.scheduler.core import Scheduler

pytestmark = pytest.mark.unit


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def state(redis):
    return RedisStateStore(redis, "scheduler")


@pytest.fixture(autouse=True)
def quick_polls(monkeypatch):
    monkeypatch.setattr(process_timer, "POLL_S", 0.01)


def test_wait_is_released_by_the_scheduler_signal(state, monkeypatch):
    monkeypatch.setattr(process_timer.service, "ensure_running", Mock())
    with process_timer.Timer(2, state=state) as timer:
        entry = timer.entry
        assert entry.record["delay"] == 2
        assert entry.record["owner"] == "process"
        assert entry.record["state"] == "active"
        assert entry.record["slots"] == []
        scheduler = Scheduler(state)
        assert scheduler.run_due(now=entry.record["due"] - 1) == 0
        assert entry.state == "active"
        assert scheduler.run_due(now=entry.record["due"]) == 1
        timer.wait()
    assert entry.state == "cancelled"
    process_timer.service.ensure_running.assert_called_once_with()


class _Entry:
    def __init__(self, fired_after: int):
        self.reads, self.fired_after = 0, fired_after

    @property
    def state(self):
        self.reads += 1
        return "fired" if self.reads > self.fired_after else "active"


def _bare_timer(entry):
    from threading import Event

    timer = process_timer.Timer.__new__(process_timer.Timer)
    timer.entry, timer.cancelled = entry, Event()
    return timer


def test_a_wait_with_no_ring_by_its_recheck_starts_the_scheduler_again(monkeypatch):
    ensure = Mock()
    monkeypatch.setattr(process_timer.service, "ensure_running", ensure)
    monkeypatch.setattr(process_timer, "RECHECK_S", 0.03)
    timer = _bare_timer(_Entry(fired_after=40))
    timer.wait()
    assert timer.entry.reads == 41
    assert ensure.call_count >= 1


def test_a_bounded_wait_with_no_ring_returns_at_its_deadline(monkeypatch):
    ensure = Mock()
    monkeypatch.setattr(process_timer.service, "ensure_running", ensure)
    timer = _bare_timer(_Entry(fired_after=10**9))
    started = time.monotonic()
    assert timer.wait(timeout=0.1) is None
    assert 0.1 <= time.monotonic() - started < process_timer.RECHECK_S
    ensure.assert_not_called()


def test_a_removed_entry_ends_the_wait():
    class Gone:
        @property
        def state(self):
            raise KeyError("gone")

    _bare_timer(Gone()).wait(timeout=5)


def test_wait_passes_its_bound_to_the_timer(state, monkeypatch):
    bounds = []
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    monkeypatch.setattr(process_timer.Timer, "wait", lambda timer, timeout=None: bounds.append(timeout))
    process_timer.wait(2, state=state, timeout=3)
    assert bounds == [3]


def test_interruption_cancels_the_timer(state, monkeypatch):
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    with pytest.raises(KeyboardInterrupt), process_timer.Timer(3, state=state) as timer:
        raise KeyboardInterrupt
    assert timer.entry.state == "cancelled"
    assert Scheduler(state).run_due(now=timer.entry.record["due"]) == 0


def test_a_timer_without_a_store_opens_the_scheduler_bucket(state, monkeypatch):
    from scripts.events import state as states

    opened = []
    monkeypatch.setattr(states, "open_state", lambda bucket: opened.append(bucket) or state)
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    with process_timer.Timer(0) as timer:
        Scheduler(state).run_due(now=timer.entry.record["due"])
        timer.wait()
    assert opened == ["scheduler"]


async def test_async_wait_cancels_its_timer_and_unblocks_its_worker(state, monkeypatch):
    import asyncio

    started = asyncio.Event()
    timers = []
    original = process_timer.Timer

    def timer(delay):
        result = original(delay, state=state)
        timers.append(result)
        started.set()
        return result

    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    monkeypatch.setattr(process_timer, "Timer", timer)
    task = asyncio.create_task(process_timer.async_wait(60))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert timers[0].cancelled.is_set()
    assert timers[0].entry.state == "cancelled"


@pytest.mark.parametrize("deployment", ["cloud", "local"])
def test_member_timer_writes_only_its_writable_subtree(redis, monkeypatch, deployment):
    from fnmatch import fnmatchcase

    from scripts.hive.auth import _acl_keys
    from scripts.scheduler.core import bucket

    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", deployment)
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "member")
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    assert bucket() == ("scheduler-member" if deployment != "local" else "scheduler")
    state = RedisStateStore(redis, bucket())
    with process_timer.Timer(15, state=state) as timer:
        if deployment != "local":
            writable = [pattern[1:] for pattern in _acl_keys("member") if pattern.startswith("~")]
            assert redis.keys("*")
            assert all(any(fnmatchcase(key, pattern) for pattern in writable) for key in redis.keys("*"))
        Scheduler(state).run_due(now=timer.entry.record["due"])
        timer.wait()


def test_member_grants_still_refuse_shared_scheduler_writes(redis):
    from fnmatch import fnmatchcase

    from scripts.hive.auth import _acl_keys

    shared = RedisStateStore(redis, "scheduler")
    writable = [pattern[1:] for pattern in _acl_keys("member") if pattern.startswith("~")]
    assert not any(fnmatchcase(key, pattern) for key in (shared._current, shared._revision) for pattern in writable)
    other = RedisStateStore(redis, "scheduler-other")
    assert not any(fnmatchcase(other._current, pattern) for pattern in writable)


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_wait_helpers_consume_and_close_a_fired_timer(state, monkeypatch, asynchronous):
    timers = []
    original = process_timer.Timer

    def timer(delay, **given):
        assert given.get("state") is None
        result = original(delay, state=state)
        timers.append(result)
        Scheduler(state).run_due(now=result.entry.record["due"])
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


def test_wait_on_a_given_store_opens_none(state, monkeypatch):
    from scripts.events import state as states

    original = process_timer.Timer.wait

    def fired(timer, timeout=None):
        Scheduler(state).run_due(now=timer.entry.record["due"])
        original(timer, timeout)

    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    monkeypatch.setattr(states, "open_state", Mock(side_effect=AssertionError("opened its own store")))
    monkeypatch.setattr(process_timer.Timer, "wait", fired)
    process_timer.wait(2, state=state)


def test_controller_namespace_changes_only_in_hosted_mode(monkeypatch):
    from hooks._redis import redis_key
    from scripts.swarm.keyspace import ROOT

    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "cloud")
    monkeypatch.delenv("AGENTIHOOKS_HIVE_ID", raising=False)
    assert Scheduler(None, machine="worker").key("entries") == f"{ROOT}:scheduler:worker:entries"
    assert Scheduler(None, machine="worker").name("entries") == f"{ROOT}.scheduler.worker.entries"
    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT")
    assert Scheduler(None, machine="worker").key("entries") == redis_key("scheduler", "worker:entries")


def test_member_identity_does_not_change_host_keys_in_local_or_default_mode(monkeypatch):
    from hooks._redis import redis_key
    from scripts.scheduler.core import bucket

    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "member")
    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT", raising=False)
    assert Scheduler(None, machine="worker").key("entries") == redis_key("scheduler", "worker:entries")
    assert bucket() == "scheduler"
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    assert Scheduler(None, machine="worker").key("entries") == redis_key("scheduler", "worker:entries")
    assert bucket() == "scheduler"


def test_simultaneous_process_timers_ring_independently(state, monkeypatch):
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    with process_timer.Timer(0, state=state) as first, process_timer.Timer(60, state=state) as second:
        assert first.entry.id != second.entry.id
        Scheduler(state).run_due(now=first.entry.record["due"])
        first.wait()
        assert second.entry.state == "active"
        assert second.wait(timeout=0.05) is None


def test_process_timer_records_are_bounded_across_repeated_waits(state, monkeypatch):
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    scheduler = Scheduler(state)
    for _ in range(20):
        with process_timer.Timer(0, state=state) as timer:
            scheduler.run_due(now=timer.entry.record["due"])
            timer.wait()
        scheduler.run_due()
        assert scheduler.document()["entries"] == {}
        assert scheduler.document()["retired"] == {}


def test_a_scheduler_that_cannot_start_leaves_no_timer_behind(state, monkeypatch):
    def unwritable():
        raise PermissionError("cache folder")

    monkeypatch.setattr(process_timer.service, "ensure_running", unwritable)
    scheduler = Scheduler(state)
    with pytest.raises(PermissionError):
        process_timer.Timer(5, state=state)
    assert len(scheduler.document()["retired"]) == 1
    scheduler.run_due()
    assert scheduler.document()["entries"] == {}


def test_a_fired_timer_survives_a_paused_consumer(state, monkeypatch):
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    with process_timer.Timer(0, state=state) as timer:
        Scheduler(state).run_due(now=timer.entry.record["due"])
        now = time.time()
        monkeypatch.setattr(time, "time", lambda: now + 120)
        timer.wait()
        assert timer.entry.state == "fired"


def test_member_schedules_and_consumes_while_shared_writes_are_refused(monkeypatch):
    import fakeredis
    from redis.exceptions import NoPermissionError

    from scripts.hive import auth
    from scripts.scheduler.core import bucket

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
    shared = RedisStateStore(admin, "scheduler")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "cloud")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "member")
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    with process_timer.Timer(0, state=RedisStateStore(member, bucket())) as timer:
        assert Scheduler(RedisStateStore(admin, bucket())).run_due(now=timer.entry.record["due"]) == 1
        timer.wait()
        assert timer.entry.state == "fired"
    Scheduler(RedisStateStore(admin, bucket())).run_due()
    with pytest.raises(NoPermissionError):
        member.hset(shared._current, "forbidden", json.dumps({"revision": 1, "value": ""}))
