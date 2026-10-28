import pytest

from scripts.events.redis_state import RedisStateStore

pytestmark = pytest.mark.unit


def test_an_interval_emits_one_interval_after_each_dispatch_and_coalesces_downtime():
    import fakeredis

    from scripts.inbox.store import InboxStore
    from scripts.scheduler.core import Scheduler
    from scripts.scheduler.slots import InboxSlot

    redis = fakeredis.FakeRedis(decode_responses=True)
    scheduler = Scheduler(RedisStateStore(redis, "scheduler"), machine="proof")
    interval = scheduler.interval(owner="operator", payload="tick", every=15, now=0)
    interval.timeout.connect(InboxSlot("session-proof"))
    assert (interval.record["kind"], interval.record["delay"], interval.record["due"]) == ("interval", 15, 15)
    assert interval.state == "ready"
    assert scheduler.run_due(now=50) == 0

    interval.start(now=100)
    assert interval.record["due"] == 115
    assert scheduler.run_due(now=114) == 0
    assert scheduler.run_due(now=115) == 1
    assert (interval.state, interval.record["due"]) == ("active", 130)
    assert scheduler.run_due(now=129) == 0
    restarted = Scheduler(RedisStateStore(redis, "scheduler"), machine="proof")
    assert restarted.run_due(now=500) == 1
    assert restarted.run_due(now=500) == 0
    assert (restarted.get(interval.id).state, restarted.get(interval.id).record["due"]) == ("active", 515)
    items = InboxStore(redis).pending_items("session-proof")
    assert [item.text for item in items] == ["tick", "tick"]
    assert items[0].id != items[1].id


def test_a_cancelled_interval_emits_no_more():
    import fakeredis

    from scripts.scheduler.core import Scheduler

    scheduler = Scheduler(RedisStateStore(fakeredis.FakeRedis(decode_responses=True), "scheduler"), machine="proof")
    interval = scheduler.interval(owner="operator", payload="tick", every=15, now=0)
    interval.start(now=0)
    assert scheduler.run_due(now=15) == 1
    interval.cancel()

    assert scheduler.run_due(now=60) == 0
    assert interval.state == "cancelled"


def test_an_interval_defaults_to_the_wall_clock(monkeypatch):
    import fakeredis

    from scripts.scheduler import core

    monkeypatch.setattr(core.time, "time", lambda: 1000.0)
    scheduler = core.Scheduler(
        RedisStateStore(fakeredis.FakeRedis(decode_responses=True), "scheduler"), machine="proof"
    )

    assert scheduler.interval(owner="operator", payload="tick", every=15).record["due"] == 1015.0


@pytest.mark.parametrize("every", [0, -1])
def test_an_interval_must_be_positive(every):
    import fakeredis

    from scripts.scheduler.core import Scheduler

    scheduler = Scheduler(RedisStateStore(fakeredis.FakeRedis(decode_responses=True), "scheduler"), machine="proof")
    with pytest.raises(ValueError) as refused:
        scheduler.interval(owner="operator", payload="tick", every=every, now=0)

    assert str(refused.value) == "an interval must be positive"
    assert scheduler.document()["entries"] == {}


def test_an_interval_of_one_second_is_accepted():
    import fakeredis

    from scripts.scheduler.core import Scheduler

    scheduler = Scheduler(RedisStateStore(fakeredis.FakeRedis(decode_responses=True), "scheduler"), machine="proof")

    assert scheduler.interval(owner="operator", payload="tick", every=1, now=0).record["due"] == 1
