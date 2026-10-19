import pytest

pytestmark = pytest.mark.unit


def test_owned_lists_every_entry_of_one_owner_in_any_state():
    import fakeredis

    from scripts.scheduler.core import Scheduler

    scheduler = Scheduler(fakeredis.FakeRedis(decode_responses=True), machine="proof")
    ready = scheduler.interval(owner="terminal", payload="tick", every=15, now=0)
    cancelled = scheduler.timer(owner="terminal", payload="once", delay=5, now=0)
    cancelled.cancel()
    scheduler.timer(owner="operator", payload="other", delay=5, now=0)

    assert sorted(entry.id for entry in scheduler.owned("terminal")) == sorted([ready.id, cancelled.id])
    assert {entry.state for entry in scheduler.owned("terminal")} == {"ready", "cancelled"}
    assert scheduler.owned("nobody") == []
