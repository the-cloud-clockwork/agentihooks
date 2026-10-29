from unittest.mock import Mock

import pytest

from scripts.events.redis_state import RedisStateStore
from tests.scheduler import leases

pytestmark = pytest.mark.unit


@pytest.fixture
def scheduler():
    import fakeredis

    from scripts.scheduler.core import Scheduler

    return Scheduler(RedisStateStore(fakeredis.FakeRedis(decode_responses=True), "scheduler"), machine="proof")


def test_child_can_acquire_supervisor_ownership_before_spawn_returns(monkeypatch, tmp_path):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)
    accepted = []

    def spawn(*args, **kwargs):
        with service.machine_lock("supervisor"):
            accepted.append("child acquired ownership")
        return Mock()

    monkeypatch.setattr(service, "subprocess", Mock(Popen=spawn))
    assert service.ensure_running({"REDIS_URL": "redis://proof.invalid/0"}) is True
    assert accepted == ["child acquired ownership"]


def test_due_now_timer_waits_until_connections_are_ready(scheduler):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler.slots import InboxSlot

    timer = scheduler.timer(owner="operator", payload="ready", delay=0, now=100)
    assert scheduler.run_due(now=100) == 0
    timer.timeout.connect(InboxSlot("proof-session"))
    assert scheduler.run_due(now=101) == 0
    timer.start(now=102)
    assert scheduler.run_due(now=102) == 1
    assert [item.text for item in InboxStore(scheduler.state.redis).pending_items("proof-session")] == ["ready"]
    with pytest.raises(ValueError, match="^only a ready schedule can be started$"):
        timer.start(now=100)


@pytest.mark.parametrize("state,due", [("cancelled", 100), ("active", 120)])
def test_stale_due_index_cannot_claim_an_inactive_or_future_entry(scheduler, state, due):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler.slots import InboxSlot

    entry = scheduler.timer(owner="operator", payload="stale", at=due, now=100)
    entry.timeout.connect(InboxSlot("proof-session"))
    entry.start(now=100)
    if state == "cancelled":
        entry.cancel()
    assert scheduler.run_due(now=110) == 0
    assert entry.state == state
    assert InboxStore(scheduler.state.redis).pending_items("proof-session") == []


@pytest.mark.parametrize("kind", ["timer", "stopwatch"])
def test_cancel_or_stop_discards_a_claimed_but_undelivered_signal(monkeypatch, scheduler, kind):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler.slots import InboxSlot

    if kind == "timer":
        entry = scheduler.timer(owner="operator", payload="cancelled", delay=1, now=100)
        entry.timeout.connect(InboxSlot("proof-session"))
        entry.start(now=100)
    else:
        entry = scheduler.stopwatch(owner="operator", payload="cancelled", limit=1, now=100)
        entry.limit_reached.connect(InboxSlot("proof-session"))
        entry.start(now=100)

    def crash(*args):
        raise RuntimeError("crash before transport")

    with monkeypatch.context() as fault:
        fault.setattr(InboxSlot, "deliver", crash)
        assert scheduler.run_due(now=101) == 1
    assert entry.id not in leases.due(scheduler)
    assert leases.pending(scheduler) == {entry.id}
    if kind == "timer":
        entry.cancel()
        expected = "cancelled"
    else:
        entry.stop(now=102)
        expected = "stopped"
    assert scheduler.run_due(now=110) == 0
    assert entry.state == expected
    assert leases.pending(scheduler) == set()
    assert InboxStore(scheduler.state.redis).pending_items("proof-session") == []


def test_cron_cancelled_inside_a_slot_never_reactivates_or_calls_later_slots(monkeypatch, scheduler):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler.slots import InboxSlot

    cron = scheduler.cron(owner="operator", payload="tick", expression="* * * * *", now=0)
    cron.timeout.connect(InboxSlot("first-slot"))
    cron.timeout.connect(InboxSlot("later-slot"))
    cron.start(now=0)
    original = InboxSlot.deliver

    def cancel_after_acceptance(self, event, delivery_id, redis):
        original(self, event, delivery_id, redis)
        cron.cancel()

    monkeypatch.setattr(InboxSlot, "deliver", cancel_after_acceptance)
    assert scheduler.run_due(now=60) == 1
    assert cron.state == "cancelled"
    inbox = InboxStore(scheduler.state.redis)
    assert [item.text for item in inbox.pending_items("first-slot")] == ["tick"]
    assert inbox.pending_items("later-slot") == []
    assert scheduler.run_due(now=120) == 0


def test_lost_lease_cannot_acknowledge_accepted_delivery(monkeypatch, scheduler):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler.slots import InboxSlot

    timer = scheduler.timer(owner="operator", payload="once", delay=1, now=100)
    timer.timeout.connect(InboxSlot("proof-session"))
    timer.start(now=100)
    original = InboxSlot.deliver

    def lose_ownership(self, event, delivery_id, state):
        original(self, event, delivery_id, state)
        leases.steal(scheduler)

    with monkeypatch.context() as fault:
        fault.setattr(InboxSlot, "deliver", lose_ownership)
        with pytest.raises(RuntimeError, match="^scheduler lost its dispatch lock$"):
            scheduler.run_due(now=101)
    assert timer.state == "pending"
    assert timer.record["pending"]["delivered"] == []
    leases.free(scheduler)
    assert scheduler.run_due(now=110) == 0
    assert timer.state == "fired"
    assert len(InboxStore(scheduler.state.redis).pending_items("proof-session")) == 1


def test_lost_lease_after_acknowledgment_cannot_finish_the_occurrence(monkeypatch, scheduler):
    from scripts.scheduler.slots import InboxSlot

    timer = scheduler.timer(owner="operator", payload="once", delay=1, now=100)
    timer.timeout.connect(InboxSlot("proof-session"))
    timer.start(now=100)
    original = scheduler.update

    def lose_after_ack(entry_id, change, *, lease=None):
        original(entry_id, change, lease=lease)
        if lease is not None:
            leases.steal(scheduler)

    with monkeypatch.context() as fault:
        fault.setattr(scheduler, "update", lose_after_ack)
        with pytest.raises(RuntimeError, match="^scheduler lost its dispatch lock$"):
            scheduler.run_due(now=101)
    assert timer.state == "pending"
    leases.free(scheduler)
    assert scheduler.run_due(now=110) == 0
    assert timer.state == "fired"


def _race_first_write(monkeypatch, scheduler, race):
    update = scheduler.state.update
    raced = []

    def raced_update(key, value, revision):
        if not raced and b'"pending"' in value:
            raced.append(True)
            race()
        return update(key, value, revision)

    monkeypatch.setattr(scheduler.state, "update", raced_update)
    return raced


def test_lease_loss_before_claim_commit_keeps_the_entry_unclaimed(monkeypatch, scheduler):
    timer = scheduler.timer(owner="operator", payload="notice", delay=1, now=100)
    timer.start(now=100)

    with monkeypatch.context() as fault:
        assert _race_first_write(fault, scheduler, lambda: leases.steal(scheduler)) == []
        with pytest.raises(RuntimeError, match="^scheduler lost its dispatch lock$"):
            scheduler.run_due(now=101)
    assert timer.state == "active"
    leases.free(scheduler)
    assert scheduler.run_due(now=110) == 1
    assert timer.state == "fired"


def test_concurrent_cancellation_before_claim_commit_is_preserved(monkeypatch, scheduler):
    timer = scheduler.timer(owner="operator", payload="notice", delay=1, now=100)
    timer.start(now=100)
    raced = _race_first_write(monkeypatch, scheduler, timer.cancel)
    assert scheduler.run_due(now=101) == 0
    assert raced == [True]
    assert timer.state == "cancelled"


def test_start_retries_a_concurrent_entry_update(monkeypatch, scheduler):
    from scripts.events.bus import StateConflict

    timer = scheduler.timer(owner="operator", payload="notice", delay=1, now=100)
    update = scheduler.state.update
    raced = []

    def raced_update(key, value, revision):
        if not raced:
            raced.append(True)
            raise StateConflict("concurrent entry update")
        return update(key, value, revision)

    monkeypatch.setattr(scheduler.state, "update", raced_update)
    timer.start(now=100)
    assert raced == [True]
    assert timer.state == "active"
    assert timer.record["started_at"] == 100
    assert scheduler.run_due(now=101) == 1
