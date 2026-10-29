import pytest

from scripts.events.redis_state import RedisStateStore

pytestmark = pytest.mark.unit


def test_timer_signal_survives_restart_and_delivers_once():
    import fakeredis

    from scripts.inbox.store import InboxStore
    from scripts.scheduler.core import Scheduler
    from scripts.scheduler.slots import InboxSlot

    redis = fakeredis.FakeRedis(decode_responses=True)
    scheduler = Scheduler(RedisStateStore(redis, "scheduler"), machine="proof")
    timer = scheduler.timer(owner="operator", delay=5, payload="time expired", now=100)
    timer.timeout.connect(InboxSlot("session-proof"))
    timer.start(now=100)
    assert scheduler.run_due(now=104) == 0
    restarted = Scheduler(RedisStateStore(redis, "scheduler"), machine="proof")
    assert restarted.run_due(now=110) == 1
    assert restarted.run_due(now=111) == 0
    inbox = InboxStore(redis)
    items = inbox.pending_items("session-proof")
    assert len(items) == 1
    assert items[0].text == "time expired"
    assert items[0].sender == "operator"
    assert restarted.get(timer.id).state == "fired"
