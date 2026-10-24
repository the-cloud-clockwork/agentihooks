import logging
import re
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def scheduler():
    import fakeredis

    from scripts.scheduler.core import Scheduler

    return Scheduler(fakeredis.FakeRedis(decode_responses=True), machine="proof")


def test_default_clock_and_machine_namespace(monkeypatch):
    import fakeredis

    from hooks._redis import redis_key
    from scripts.scheduler import core
    from scripts.scheduler.slots import InboxSlot

    monkeypatch.setattr(core, "time", SimpleNamespace(time=lambda: 100))
    monkeypatch.setattr(core, "socket", SimpleNamespace(gethostname=lambda: "default-machine"))
    redis = fakeredis.FakeRedis(decode_responses=True)
    scheduler = core.Scheduler(redis)
    assert scheduler.key("entries") == redis_key("scheduler", "default-machine:entries")
    timer = scheduler.timer(owner="operator", payload="notice", delay=0)
    timer.timeout.connect(InboxSlot("proof-session"))
    timer.start(now=100)
    assert timer.record["id"] == timer.id
    assert timer.record["kind"] == "timer"
    assert timer.record["started_at"] == 100
    assert timer.record["due"] == 100
    assert timer.record["owner"] == "operator"
    assert timer.record["payload"] == "notice"
    assert core.Scheduler(redis, machine="other-machine").run_due() == 0
    assert scheduler.run_due() == 1
    assert timer.state == "fired"


def test_named_signals_only_call_their_own_connected_slots(scheduler):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler.slots import InboxSlot

    timer = scheduler.timer(owner="operator", payload="timeout", delay=1, now=100)
    timer.limit_reached.connect(InboxSlot("wrong-signal"))
    timer.timeout.connect(InboxSlot("right-signal"))
    timer.start(now=100)
    assert scheduler.run_due(now=101) == 1
    inbox = InboxStore(scheduler.redis)
    assert inbox.pending_items("wrong-signal") == []
    assert [item.text for item in inbox.pending_items("right-signal")] == ["timeout"]


def test_signal_disconnect_and_timer_cancel(scheduler):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler.slots import InboxSlot

    timer = scheduler.timer(owner="operator", payload="notice", delay=1, now=100)
    connection = timer.timeout.connect(InboxSlot("disconnected"))
    timer.timeout.connect(InboxSlot("connected"))
    timer.timeout.disconnect(connection)
    timer.start(now=100)
    assert scheduler.run_due(now=101) == 1
    inbox = InboxStore(scheduler.redis)
    assert inbox.pending_items("disconnected") == []
    assert [item.text for item in inbox.pending_items("connected")] == ["notice"]
    cancelled = scheduler.timer(owner="operator", payload="cancelled", at=110, now=100)
    cancelled.timeout.connect(InboxSlot("cancelled"))
    cancelled.start(now=100)
    cancelled.cancel()
    assert cancelled.state == "cancelled"
    assert scheduler.redis.zcard(scheduler.key("due")) == 0
    assert scheduler.run_due(now=120) == 0
    assert inbox.pending_items("cancelled") == []


def test_inbox_slot_delivers_to_a_seat_after_its_occupant_changes(scheduler):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler.slots import InboxSlot

    inbox = InboxStore(scheduler.redis)
    seat = "eng-1@proof"
    inbox.seats.occupy(seat, "first-session", 1)
    timer = scheduler.timer(owner="operator", payload="seat notice", delay=5, now=100)
    timer.timeout.connect(InboxSlot(seat))
    timer.start(now=100)
    inbox.seats.occupy(seat, "next-session", 2)
    assert scheduler.run_due(now=105) == 1
    items = inbox.pending_items(seat)
    assert len(items) == 1
    assert items[0].address == seat
    assert items[0].text == "seat notice"
    assert inbox.seats.occupant(seat).occupant == "next-session"


def test_inbox_retry_after_transport_acceptance_keeps_one_item(monkeypatch, scheduler):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler.core import Scheduler
    from scripts.scheduler.slots import InboxSlot

    timer = scheduler.timer(owner="operator", payload="once", delay=1, now=100)
    timer.timeout.connect(InboxSlot("proof-session"))
    timer.start(now=100)
    original = InboxSlot.deliver

    def crash(self, event, delivery_id, redis):
        original(self, event, delivery_id, redis)
        raise RuntimeError("transport accepted before crash")

    with monkeypatch.context() as fault:
        fault.setattr(InboxSlot, "deliver", crash)
        assert scheduler.run_due(now=101) == 1
    inbox = InboxStore(scheduler.redis)
    first = inbox.pending_items("proof-session")
    assert len(first) == 1
    assert Scheduler(scheduler.redis, machine="proof").run_due(now=110) == 0
    assert inbox.pending_items("proof-session") == first
    assert timer.state == "fired"


def test_retry_continues_after_an_already_acknowledged_slot(monkeypatch, scheduler):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler.slots import InboxSlot

    timer = scheduler.timer(owner="operator", payload="both", delay=1, now=100)
    first_connection = timer.timeout.connect(InboxSlot("first-session"))
    timer.timeout.connect(InboxSlot("second-session"))
    timer.start(now=100)
    original = InboxSlot.deliver

    def crash_second(self, event, delivery_id, redis):
        if self.address == "second-session":
            raise RuntimeError("second slot unavailable")
        original(self, event, delivery_id, redis)

    with monkeypatch.context() as fault:
        fault.setattr(InboxSlot, "deliver", crash_second)
        assert scheduler.run_due(now=101) == 1
    inbox = InboxStore(scheduler.redis)
    assert len(inbox.pending_items("first-session")) == 1
    assert inbox.pending_items("second-session") == []
    assert timer.record["pending"]["delivered"] == [first_connection]
    assert scheduler.run_due(now=110) == 0
    assert len(inbox.pending_items("first-session")) == 1
    assert [item.text for item in inbox.pending_items("second-session")] == ["both"]
    assert timer.state == "fired"


def test_a_failing_slot_is_logged_unacknowledged_and_other_entries_still_run(caplog, scheduler):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler.slots import CallableSlot, InboxSlot

    reference = "scripts.scheduler.absent_slot_module:deliver"
    broken = scheduler.timer(owner="operator", payload="broken", delay=1, now=100)
    broken.timeout.connect(CallableSlot(reference))
    sibling = broken.timeout.connect(InboxSlot("same-entry-session"))
    broken.start(now=100)
    healthy = scheduler.timer(owner="operator", payload="healthy", delay=1, now=100)
    healthy.timeout.connect(InboxSlot("proof-session"))
    healthy.start(now=100)
    with caplog.at_level(logging.WARNING, logger="scripts.scheduler.core"):
        assert scheduler.run_due(now=101) == 2
    inbox = InboxStore(scheduler.redis)
    assert [item.text for item in inbox.pending_items("proof-session")] == ["healthy"]
    assert healthy.state == "fired"
    assert [item.text for item in inbox.pending_items("same-entry-session")] == ["broken"]
    assert broken.state == "pending"
    assert broken.record["pending"]["delivered"] == [sibling]
    assert scheduler.redis.smembers(scheduler.key("pending")) == {broken.id}
    assert [record.getMessage() for record in caplog.records] == [
        f"scheduler slot delivery failed: entry {broken.id} slot {{'kind': 'callable', 'reference': '{reference}'}}"
        " error ModuleNotFoundError(\"No module named 'scripts.scheduler.absent_slot_module'\")"
    ]
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="scripts.scheduler.core"):
        assert scheduler.run_due(now=110) == 0
    assert broken.state == "pending"
    assert broken.record["pending"]["delivered"] == [sibling]
    assert len(inbox.pending_items("same-entry-session")) == 1
    assert len(caplog.records) == 1


def test_a_pending_entry_without_pending_details_is_logged_as_stale_and_the_pass_continues(caplog, scheduler):
    import json

    from scripts.inbox.store import InboxStore
    from scripts.scheduler.slots import InboxSlot

    stale = scheduler.timer(owner="operator", payload="stale", delay=1, now=100)
    stored = {**stale.record, "state": "pending"}
    scheduler.redis.hset(scheduler.key("entries"), stale.id, json.dumps(stored))
    scheduler.redis.sadd(scheduler.key("pending"), stale.id)
    healthy = scheduler.timer(owner="operator", payload="healthy", delay=1, now=100)
    healthy.timeout.connect(InboxSlot("proof-session"))
    healthy.start(now=100)
    with caplog.at_level(logging.WARNING, logger="scripts.scheduler.core"):
        assert scheduler.run_due(now=101) == 1
    assert [item.text for item in InboxStore(scheduler.redis).pending_items("proof-session")] == ["healthy"]
    assert healthy.state == "fired"
    assert stale.record == stored
    assert [record.getMessage() for record in caplog.records] == [
        f"scheduler skipped stale pending entry {stale.id}: no pending details"
    ]
    assert scheduler.redis.smembers(scheduler.key("pending")) == set()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="scripts.scheduler.core"):
        assert scheduler.run_due(now=110) == 0
    assert caplog.records == []


def test_a_stale_pending_entry_stays_indexed_when_the_dispatch_lease_is_lost(monkeypatch, scheduler):
    import json

    from scripts.scheduler import core

    stale = scheduler.timer(owner="operator", payload="stale", delay=1, now=100)
    scheduler.redis.hset(scheduler.key("entries"), stale.id, json.dumps({**stale.record, "state": "pending"}))
    scheduler.redis.sadd(scheduler.key("pending"), stale.id)
    lock_key = scheduler.key("dispatch-lock")

    def lose_ownership(*args):
        scheduler.redis.set(lock_key, "successor")

    monkeypatch.setattr(core._log, "warning", lose_ownership)
    with pytest.raises(RuntimeError, match="^scheduler lost its dispatch lock$"):
        scheduler.run_due(now=101)
    assert scheduler.redis.smembers(scheduler.key("pending")) == {stale.id}


def test_dispatch_lock_refuses_a_competing_drain_without_losing_work(scheduler):
    timer = scheduler.timer(owner="operator", payload="notice", delay=1, now=100)
    timer.start(now=100)
    lock = scheduler.redis.lock(scheduler.key("dispatch-lock"), timeout=30)
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="^another scheduler is dispatching on this machine$"):
            scheduler.run_due(now=101)
    finally:
        lock.release()
    assert timer.state == "active"
    assert scheduler.run_due(now=101) == 1
    assert timer.state == "fired"


def test_missing_schedule_reports_its_identity(scheduler):
    with pytest.raises(KeyError) as read:
        scheduler.get("absent")
    assert read.value.args == ("absent",)
    with pytest.raises(KeyError) as update:
        scheduler.update("absent", lambda entry: None)
    assert update.value.args == ("absent",)


def test_update_indexes_a_pending_entry_for_restart(scheduler):
    timer = scheduler.timer(owner="operator", payload="durable", delay=1, now=100)
    timer.start(now=100)

    def materialize(entry):
        entry["state"] = "pending"
        entry["pending"] = {
            "id": "recovered-occurrence",
            "scheduled_at": 101,
            "fired_at": 110,
            "slots": [],
            "delivered": [],
        }

    scheduler.update(timer.id, materialize)
    assert scheduler.redis.zcard(scheduler.key("due")) == 0
    assert scheduler.redis.smembers(scheduler.key("pending")) == {timer.id}
    assert scheduler.run_due(now=120) == 0
    assert timer.state == "fired"
    assert scheduler.redis.scard(scheduler.key("pending")) == 0


def test_zero_limit_stopwatch_fires_once(scheduler):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler.slots import InboxSlot

    watch = scheduler.stopwatch(owner="operator", payload="zero", limit=0, now=100)
    watch.limit_reached.connect(InboxSlot("proof-session"))
    watch.start(now=100)
    assert scheduler.run_due(now=100) == 1
    assert scheduler.run_due(now=100) == 0
    assert watch.elapsed(now=100) == 0
    assert [item.text for item in InboxStore(scheduler.redis).pending_items("proof-session")] == ["zero"]


@pytest.mark.parametrize("kind,option", [("timer", "delay"), ("stopwatch", "limit")])
def test_relative_entry_records_its_deadline_preview_before_start(scheduler, kind, option):
    entry = getattr(scheduler, kind)(owner="operator", payload="preview", now=100, **{option: 5})
    assert entry.state == "ready"
    assert entry.record["due"] == 105
    assert entry.record["started_at"] is None


def test_a_drain_reports_each_due_entry(scheduler):
    first = scheduler.timer(owner="operator", payload="first", delay=1, now=100)
    second = scheduler.stopwatch(owner="operator", payload="second", limit=1, now=100)
    first.start(now=100)
    second.start(now=100)
    assert scheduler.run_due(now=101) == 2
    assert first.state == second.state == "fired"
    assert scheduler.run_due(now=101) == 0


@pytest.mark.parametrize(
    "options,message",
    [
        ({}, "a timer needs either delay or at"),
        ({"delay": 1, "at": 100}, "a timer needs either delay or at"),
        ({"delay": -1}, "delay must not be negative"),
        ({"delay": 1, "owner": ""}, "a schedule needs an owner and payload"),
        ({"delay": 1, "payload": " "}, "a schedule needs an owner and payload"),
    ],
)
def test_timer_refuses_invalid_entries(scheduler, options, message):
    values = {"owner": "operator", "payload": "notice", "now": 100, **options}
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        scheduler.timer(**values)


def test_stopwatch_default_clock_and_negative_limit(monkeypatch, scheduler):
    from scripts.scheduler import core

    clock = [100]
    monkeypatch.setattr(core, "time", SimpleNamespace(time=lambda: clock[0]))
    watch = scheduler.stopwatch(owner="operator", payload="limit", limit=10)
    watch.start(now=100)
    clock[0] = 104
    assert watch.elapsed() == 4
    watch.stop()
    clock[0] = 120
    assert watch.elapsed() == 4
    assert scheduler.run_due() == 0
    with pytest.raises(ValueError, match="^limit must not be negative$"):
        scheduler.stopwatch(owner="operator", payload="limit", limit=-1)


def test_cron_requires_five_fields_and_uses_the_default_clock(monkeypatch, scheduler):
    from scripts.scheduler import core

    monkeypatch.setattr(core, "time", SimpleNamespace(time=lambda: 0))
    assert scheduler.cron(owner="operator", payload="tick", expression="* * * * *").record["due"] == 60
    with pytest.raises(ValueError, match="^a cron expression needs five fields$"):
        scheduler.cron(owner="operator", payload="tick", expression="* * * * * *")
