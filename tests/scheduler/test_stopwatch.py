import pytest

pytestmark = pytest.mark.unit


def test_stopwatch_elapsed_and_limit_survive_restart():
    import fakeredis

    from scripts.inbox.store import InboxStore
    from scripts.scheduler.core import Scheduler
    from scripts.scheduler.slots import InboxSlot

    redis = fakeredis.FakeRedis(decode_responses=True)
    scheduler = Scheduler(redis, machine="proof")
    watch = scheduler.stopwatch(owner="operator", limit=10, payload="limit crossed", now=100)
    watch.limit_reached.connect(InboxSlot("session-proof"))
    watch.start(now=100)
    assert watch.elapsed(now=104) == 4
    assert watch.limit_reached.name == "limit_reached"
    assert scheduler.run_due(now=109) == 0
    restarted = Scheduler(redis, machine="proof")
    assert restarted.get(watch.id).elapsed(now=109) == 9
    assert restarted.run_due(now=110) == 1
    assert restarted.run_due(now=120) == 0
    assert restarted.get(watch.id).elapsed(now=120) == 10
    assert [item.text for item in InboxStore(redis).pending_items("session-proof")] == ["limit crossed"]


def test_stopped_stopwatch_records_elapsed_without_firing():
    import fakeredis

    from scripts.inbox.store import InboxStore
    from scripts.scheduler.core import Scheduler
    from scripts.scheduler.slots import InboxSlot

    redis = fakeredis.FakeRedis(decode_responses=True)
    scheduler = Scheduler(redis, machine="proof")
    watch = scheduler.stopwatch(owner="operator", limit=10, payload="limit crossed", now=100)
    watch.limit_reached.connect(InboxSlot("session-proof"))
    watch.start(now=100)
    watch.stop(now=103)
    assert watch.elapsed(now=120) == 3
    assert watch.state == "stopped"
    assert scheduler.run_due(now=120) == 0
    assert InboxStore(redis).pending_items("session-proof") == []
