import json

import pytest

from scripts.events.config import BusConfig
from scripts.events.memory import open_bus


def reading(cap=1, sessions=0):
    return {
        "slots": [{"harness": "claude", "account": "a", "cap": cap}],
        "sessions": {"claude:a": sessions},
    }


@pytest.fixture
def rig():
    from scripts.hive import account_slots

    now = [1000]
    bus = open_bus(BusConfig(), clock=lambda: now[0])
    report = [reading()]
    first = account_slots.Slots(bus, "one", lambda: report[0], lambda: now[0])
    second = account_slots.Slots(bus, "two", lambda: reading(), lambda: now[0])
    yield first, second, now, report, bus
    bus.close()


def test_account_capacity_is_shared_between_hives_and_returned_on_exit(rig):
    first, second, _, _, _ = rig
    assert first.take("execution-one", "claude") == "a"
    assert second.take("execution-two", "claude") is None
    first.release("execution-one", "claude", "a")
    assert second.take("execution-two", "claude") == "a"


def test_a_counter_is_taken_before_launch_and_returned_on_failure(rig):
    first, second, _, _, _ = rig

    def launch(account):
        assert account == "a"
        assert second.take("other", "claude") is None
        raise RuntimeError("launch failed")

    with pytest.raises(RuntimeError, match="^launch failed$"):
        first.launch("execution-one", "claude", "", launch)
    assert second.take("other", "claude") == "a"


def test_heartbeat_renews_occupied_slots_beyond_the_original_deadline(rig):
    from scripts.hive import account_slots

    first, second, now, _, _ = rig
    assert first.take("execution-one", "claude") == "a"
    now[0] += account_slots.TTL_MS - 1
    first.heartbeat(reading(), {"execution-one": {"pane": {"state": "working"}}})
    now[0] += 2
    assert second.take("execution-two", "claude") is None
    now[0] += account_slots.TTL_MS
    assert second.take("execution-two", "claude") == "a"


def test_fresh_quota_closes_launches_without_dropping_live_ownership(rig):
    first, second, _, report, _ = rig
    assert first.take("one", "claude") == "a"
    report[0] = reading(cap=0)
    first.heartbeat(report[0], {"one": {"pane": {"state": "working"}}})
    assert first.take("two", "claude") is None
    assert second.take("two", "claude") is None
    first.release("one", "claude", "a")
    assert first.take("two", "claude") is None


def test_local_manual_sessions_and_reserved_slots_are_counted_once(rig):
    first, second, _, report, _ = rig
    report[0] = reading(cap=3, sessions=1)
    assert first.take("one", "claude") == "a"
    first.heartbeat(reading(cap=3, sessions=2), {"one": {"pane": {"state": "working"}}})
    second.read = lambda: reading(cap=3)
    assert second.take("two", "claude") == "a"
    assert second.take("three", "claude") is None


def test_an_exit_observation_releases_even_when_the_quota_report_has_no_accounts(rig):
    first, second, _, _, _ = rig
    assert first.take("one", "claude") == "a"
    first.heartbeat({"slots": [], "sessions": {}}, {"one": {"pane": {"state": "exited"}}})
    assert second.take("two", "claude") == "a"


def test_live_slot_replay_is_idempotent_and_foreign_release_is_ignored(rig):
    first, second, _, _, _ = rig
    assert first.take("one", "claude") == "a"
    assert first.take("one", "claude") == "a"
    second.release("one", "claude", "a")
    assert second.take("two", "claude") is None


def test_compare_and_set_retries_a_racing_reservation(rig, monkeypatch):
    from scripts.events.bus import StateConflict

    first, second, _, _, bus = rig
    state = bus.state("account-slots")
    create = state.create
    seen = []

    def race(key, value):
        if not seen:
            seen.append(key)
            assert second.take("other", "claude") == "a"
            raise StateConflict
        return create(key, value)

    monkeypatch.setattr(state, "create", race)
    assert first.take("one", "claude") is None


def test_counter_stores_only_account_capacity_and_execution_ownership(rig):
    from scripts.hive import account_slots

    first, _, now, _, bus = rig
    assert first.take("one", "claude", "a") == "a"
    state = json.loads(bus.state("account-slots").get(account_slots.key("claude", "a")).value)
    assert state["holders"] == {"one": {"hive": "one", "expires_ms": now[0] + account_slots.TTL_MS}}
    assert state["hives"]["one"]["cap"] == 1


def test_unavailable_account_refuses_launch_without_calling_runtime(rig):
    first, _, _, report, _ = rig
    report[0] = reading(cap=0)
    called = []
    with pytest.raises(RuntimeError, match="^No account slot available$"):
        first.launch("one", "claude", "", called.append)
    assert called == []
