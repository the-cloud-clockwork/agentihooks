import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PACKAGE = Path(__file__).resolve().parents[2] / "scripts" / "scheduler"
DELIVERED = []


@pytest.fixture
def state():
    from scripts.events import memory
    from scripts.events.config import BusConfig

    bus = memory.open_bus(BusConfig())
    try:
        yield bus.state("scheduler")
    finally:
        bus.close()


def _imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
            found.update(alias.name for alias in node.names)
    return found


def test_no_scheduler_module_imports_the_redis_client():
    offenders = {
        path.name: sorted(name for name in _imports(path) if name.split(".")[0] == "redis" or name == "redis_client")
        for path in sorted(PACKAGE.glob("*.py"))
    }
    assert {name: found for name, found in offenders.items() if found} == {}


def test_the_dispatch_lease_refuses_a_second_holder_and_frees_on_release(state):
    from scripts.scheduler.lease import dispatch_lock, verify_lease

    with dispatch_lock(state, "probe.dispatch-lock") as lease:
        verify_lease(state, lease)
        with pytest.raises(RuntimeError, match="another scheduler"):
            with dispatch_lock(state, "probe.dispatch-lock"):
                pass
    with dispatch_lock(state, "probe.dispatch-lock") as again:
        verify_lease(state, again)


def test_a_lease_taken_over_after_expiry_fails_verification(state):
    from scripts.scheduler import lease as leases

    held = leases.acquire(state, "probe.dispatch-lock", now=100.0)
    taken = leases.acquire(state, "probe.dispatch-lock", now=100.0 + leases.LEASE_SECONDS + 1)
    later = 100.0 + leases.LEASE_SECONDS + 2
    leases.verify_lease(state, taken, now=later)
    with pytest.raises(RuntimeError, match="lost its dispatch lock"):
        leases.verify_lease(state, held, now=later)


def record(event, delivery_id, state):
    DELIVERED.append((event["payload"], delivery_id, state))


def test_a_due_timer_delivers_to_its_slot_through_the_state_store(state):
    from scripts.scheduler.core import Scheduler

    DELIVERED.clear()
    scheduler = Scheduler(state, machine="probe")
    entry = scheduler.timer(owner="test", payload="ring", delay=5, now=1000.0)
    entry.timeout.connect(record)
    entry.start(now=1000.0)
    assert scheduler.run_due(now=1004.0) == 0
    assert scheduler.run_due(now=1006.0) == 1
    assert [(payload, target) for payload, _, target in DELIVERED] == [("ring", state)]
    assert entry.state == "fired"
    assert scheduler.records()[0]["id"] == entry.id


def test_a_change_that_leaves_the_sorted_document_alone_writes_nothing(state):
    from scripts.scheduler.core import Scheduler

    scheduler = Scheduler(state, machine="proof")
    key = scheduler.name("entries")
    state.create(key, b'{"retired": {}, "entries": {}}')
    assert scheduler.change(lambda document: "kept") == "kept"
    assert state.get(key).value == b'{"retired": {}, "entries": {}}'


def test_a_scheduler_write_that_never_settles_is_refused(state, monkeypatch):
    from scripts.events.bus import StateConflict
    from scripts.scheduler import core

    scheduler = core.Scheduler(state, machine="proof")
    key = scheduler.name("entries")
    attempts = []

    def conflict(name, value):
        attempts.append(name)
        raise StateConflict("raced")

    monkeypatch.setattr(state, "create", conflict)
    with pytest.raises(StateConflict) as changing:
        scheduler.change(lambda document: document["entries"].__setitem__("a", {}))
    assert str(changing.value) == f"scheduler state {key!r} kept changing"
    with pytest.raises(StateConflict) as putting:
        scheduler.put("wake", "d1")
    assert str(putting.value) == f"scheduler state {scheduler.name('wake')!r} kept changing"
    assert attempts == [key] * core.WRITE_TRIES + [scheduler.name("wake")] * core.WRITE_TRIES


def test_a_conflicting_put_retries_on_the_new_revision(state, monkeypatch):
    from scripts.events.bus import StateConflict
    from scripts.scheduler.core import Scheduler

    scheduler = Scheduler(state, machine="proof")
    create = state.create

    def racing(name, value):
        monkeypatch.setattr(state, "create", create)
        create(name, b"d0")
        raise StateConflict("raced")

    monkeypatch.setattr(state, "create", racing)
    scheduler.put("wake", "d1")
    assert scheduler.read("wake") == "d1"


def test_a_retired_timer_is_marked_retired(state):
    from scripts.scheduler.core import Scheduler

    scheduler = Scheduler(state, machine="proof")
    entry = scheduler.timer(owner="proof", payload="ring", delay=1, now=0)
    scheduler.retire(entry.id)
    assert scheduler.document()["retired"] == {entry.id: True}


def test_reaping_skips_unknown_ids_and_refuses_a_lost_lease(state):
    from scripts.scheduler.core import Scheduler
    from scripts.scheduler.lease import Lease, acquire

    scheduler = Scheduler(state, machine="proof")
    kept = scheduler.timer(owner="proof", payload="ring", delay=1, now=0)
    scheduler.change(lambda document: document["retired"].__setitem__("gone", True))
    with pytest.raises(RuntimeError, match="^scheduler lost its dispatch lock$"):
        scheduler._reap(Lease(scheduler.name("entries"), "stranger"))
    scheduler._reap(acquire(state, scheduler.name("entries")))
    assert (list(scheduler.document()["entries"]), scheduler.document()["retired"]) == ([kept.id], {})


def test_claiming_marks_only_active_entries_due_by_now(monkeypatch):
    from unittest.mock import Mock

    from scripts.scheduler import core

    monkeypatch.setattr(core.uuid, "uuid4", lambda: Mock(hex="pending-id"))
    slot = {"id": "s", "signal": "timeout"}
    document = {
        "entries": {
            "due": {"state": "active", "due": 5, "signal": "timeout", "slots": [slot, {"id": "x", "signal": "other"}]},
            "later": {"state": "active", "due": 6, "signal": "timeout", "slots": []},
            "ready": {"state": "ready", "due": 0, "signal": "timeout", "slots": []},
        }
    }
    assert core.Scheduler._claim(document, 5) == 1
    assert document["entries"] == {
        "due": {
            "state": "pending",
            "due": 5,
            "signal": "timeout",
            "slots": [slot, {"id": "x", "signal": "other"}],
            "pending": {"id": "pending-id", "scheduled_at": 5, "fired_at": 5, "slots": [slot], "delivered": []},
        },
        "later": {"state": "active", "due": 6, "signal": "timeout", "slots": []},
        "ready": {"state": "ready", "due": 0, "signal": "timeout", "slots": []},
    }
