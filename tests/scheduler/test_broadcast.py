import pytest

from scripts.events.redis_state import RedisStateStore

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("channel", [None, "scheduler-proof"])
def test_timer_broadcast_is_delivered_once_after_crash_and_clear(monkeypatch, channel):
    import fakeredis

    from hooks.context import broadcast
    from scripts.scheduler.core import Scheduler
    from scripts.scheduler.slots import BroadcastSlot

    redis = fakeredis.FakeRedis(decode_responses=True)
    scheduler = Scheduler(RedisStateStore(redis, "scheduler"), machine="proof")
    timer = scheduler.timer(owner="operator", at=100, payload="fleet notice", now=90)
    timer.timeout.connect(BroadcastSlot(channel))
    timer.start(now=100)
    original = BroadcastSlot.deliver

    def crash_after_publish(self, event, delivery_id, redis):
        original(self, event, delivery_id, redis)
        raise RuntimeError("runner died after transport accepted")

    with monkeypatch.context() as fault:
        fault.setattr(BroadcastSlot, "deliver", crash_after_publish)
        assert scheduler.run_due(now=100) == 1
    messages = broadcast.list_broadcasts()
    assert len(messages) == 1
    assert messages[0]["message"] == "fleet notice"
    assert messages[0]["source"] == "operator"
    assert messages[0]["persistent"] is False
    assert messages[0].get("channel") == channel
    delivery_id = messages[0]["id"]
    broadcast.clear_broadcasts()
    assert broadcast.list_broadcasts() == []
    restarted = Scheduler(RedisStateStore(redis, "scheduler"), machine="proof")
    assert restarted.run_due(now=110) == 0
    assert restarted.get(timer.id).state == "fired"
    assert broadcast.list_broadcasts() == []
    assert broadcast.create_broadcast("fleet notice", delivery_id=delivery_id) == delivery_id
    assert broadcast.list_broadcasts() == []


def test_publication_recognizes_a_retained_receipt_from_an_earlier_runner(tmp_path):
    import sqlite3

    from hooks.context import scheduled_broadcasts

    path = tmp_path / "broadcast.json"
    with sqlite3.connect(tmp_path / "broadcast.scheduler.sqlite3") as database:
        database.execute("CREATE TABLE emissions (id TEXT PRIMARY KEY, entry TEXT)")
        database.execute("INSERT INTO emissions VALUES (?, NULL)", ("accepted",))
    scheduled_broadcasts.publish(path, {"id": "accepted", "message": "already cleared"})
    assert scheduled_broadcasts.merge(path, []) == []


def test_scheduled_broadcast_retains_delivery_and_normal_broadcasts(monkeypatch):
    import fakeredis

    from hooks.context import broadcast
    from scripts.scheduler.core import Scheduler
    from scripts.scheduler.slots import BroadcastSlot

    redis = fakeredis.FakeRedis(decode_responses=True)
    scheduler = Scheduler(RedisStateStore(redis, "scheduler"), machine="proof")
    timer = scheduler.timer(owner="operator", delay=0, payload="scheduled", now=100)
    timer.timeout.connect(BroadcastSlot())
    timer.start(now=100)
    original = BroadcastSlot.deliver

    def crash(self, event, delivery_id, redis):
        original(self, event, delivery_id, redis)
        raise RuntimeError("crash")

    with monkeypatch.context() as fault:
        fault.setattr(BroadcastSlot, "deliver", crash)
        assert scheduler.run_due(now=100) == 1
    scheduled = broadcast.list_broadcasts()[0]
    broadcast.mark_delivered("proof-session", scheduled["id"])
    broadcast.create_broadcast("ordinary")
    Scheduler(RedisStateStore(redis, "scheduler"), machine="proof").run_due(now=110)
    messages = broadcast.list_broadcasts()
    assert len(messages) == 2
    recovered = next(item for item in messages if item["id"] == scheduled["id"])
    assert recovered["delivered_to"] == ["proof-session"]
    assert {item["message"] for item in messages} == {"ordinary", "scheduled"}


def test_broadcast_recovers_a_crash_between_fire_and_publication(monkeypatch):
    import fakeredis

    import hooks.context.scheduled_broadcasts as scheduled_broadcasts
    from hooks.context import broadcast
    from scripts.scheduler.core import Scheduler
    from scripts.scheduler.slots import BroadcastSlot

    redis = fakeredis.FakeRedis(decode_responses=True)
    scheduler = Scheduler(RedisStateStore(redis, "scheduler"), machine="proof")
    timer = scheduler.timer(owner="operator", delay=1, payload="recovered", now=100)
    timer.timeout.connect(BroadcastSlot())
    timer.start(now=100)

    def crash(*args):
        raise RuntimeError("crash before publication")

    with monkeypatch.context() as fault:
        fault.setattr(scheduled_broadcasts, "publish", crash)
        assert scheduler.run_due(now=101) == 1
    assert broadcast.list_broadcasts() == []
    restarted = Scheduler(RedisStateStore(redis, "scheduler"), machine="proof")
    assert restarted.run_due(now=120) == 0
    messages = broadcast.list_broadcasts()
    assert len(messages) == 1
    assert messages[0]["message"] == "recovered"
    assert timer.state == "fired"
    assert restarted.run_due(now=130) == 0
    assert broadcast.list_broadcasts() == messages
