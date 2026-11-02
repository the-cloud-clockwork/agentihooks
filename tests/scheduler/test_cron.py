import pytest

from scripts.events.redis_state import RedisStateStore

pytestmark = pytest.mark.unit


def test_cron_coalesces_downtime_and_continues_with_distinct_occurrences():
    import fakeredis

    from scripts.inbox.store import InboxStore
    from scripts.scheduler.core import Scheduler
    from scripts.scheduler.slots import InboxSlot

    redis = fakeredis.FakeRedis(decode_responses=True)
    scheduler = Scheduler(RedisStateStore(redis, "scheduler"), machine="proof")
    cron = scheduler.cron(owner="operator", expression="* * * * *", payload="tick", now=0)
    cron.timeout.connect(InboxSlot("session-proof"))
    cron.start(now=0)
    assert cron.record["due"] == 60
    assert cron.record["timezone"] == "UTC"
    assert scheduler.run_due(now=59) == 0
    restarted = Scheduler(RedisStateStore(redis, "scheduler"), machine="proof")
    assert restarted.run_due(now=180) == 1
    assert restarted.run_due(now=180) == 0
    assert restarted.get(cron.id).record["due"] == 240
    assert restarted.get(cron.id).state == "active"
    assert restarted.run_due(now=240) == 1
    items = InboxStore(redis).pending_items("session-proof")
    assert [item.text for item in items] == ["tick", "tick"]
    assert items[0].id != items[1].id


def test_cron_uses_standard_weekday_and_explicit_timezone():
    import fakeredis

    from scripts.scheduler.core import Scheduler
    from scripts.scheduler.model import next_due

    scheduler = Scheduler(RedisStateStore(fakeredis.FakeRedis(decode_responses=True), "scheduler"), machine="proof")
    cron = scheduler.cron(owner="operator", expression="0 9 * * 0", payload="Sunday", timezone="Europe/Madrid", now=0)
    assert next_due("0 9 * * 0", "Europe/Madrid", 0) == 288000
    assert cron.record["due"] == 288000
    assert cron.record["expression"] == "0 9 * * 0"
    assert cron.record["timezone"] == "Europe/Madrid"


@pytest.mark.parametrize("expression", ["* * * *", "* * * * * *"])
def test_calendar_rejects_expressions_outside_five_fields(expression):
    from scripts.scheduler.model import next_due

    with pytest.raises(ValueError, match="^a cron expression needs five fields$"):
        next_due(expression, "UTC", 0)
