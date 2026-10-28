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
    leases.verify_lease(state, taken)
    with pytest.raises(RuntimeError, match="lost its dispatch lock"):
        leases.verify_lease(state, held)


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
