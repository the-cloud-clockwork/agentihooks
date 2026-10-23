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


@pytest.mark.parametrize("cap,sessions,expected", [(0, 0, None), (1, 0, "a"), (1, 1, None), (2, 1, "a")])
def test_admission_uses_the_hives_current_quota_and_manual_session_count(rig, cap, sessions, expected):
    first, _, _, report, _ = rig
    report[0] = reading(cap=cap, sessions=sessions)
    assert first.take("one", "claude") == expected


def test_launch_selects_only_the_requested_harness_and_account(rig):
    first, _, _, report, _ = rig
    report[0]["slots"].insert(0, {"harness": "codex", "account": "other", "cap": 9})
    assert first.take("one", "claude", "missing") is None
    assert first.take("one", "claude", "a") == "a"
    assert first.take("two", "codex", "other") == "other"


def test_hive_restart_recovers_the_counter_index_and_releases_dead_agents(rig):
    from scripts.hive import account_slots

    first, second, now, _, bus = rig
    assert first.take("one", "claude") == "a"
    restarted = account_slots.Slots(bus, "one", reading, lambda: now[0])
    restarted.heartbeat({"slots": [], "sessions": {}}, {"one": {"pane": {"state": "dead"}}})
    assert second.take("two", "claude") == "a"


def test_slots_expire_at_the_deadline_and_not_one_millisecond_later(rig):
    from scripts.hive import account_slots

    first, second, now, _, _ = rig
    assert first.take("one", "claude") == "a"
    now[0] += account_slots.TTL_MS
    assert second.take("two", "claude") == "a"
    assert first.take("two", "claude") is None


def test_a_missing_observation_does_not_extend_the_execution_lease(rig):
    from scripts.hive import account_slots

    first, second, now, _, _ = rig
    assert first.take("one", "claude") == "a"
    now[0] += account_slots.TTL_MS - 1
    first.heartbeat(reading(), {"one": {"pane": {"state": "unknown"}}})
    now[0] += 1
    assert second.take("two", "claude") == "a"


def test_the_launch_heartbeat_keeps_the_slot_occupied_during_a_blocking_start(rig, monkeypatch):
    from scripts.hive import account_slots

    first, second, now, _, _ = rig
    intervals = []

    class Interval:
        def __init__(self, callback, *, every):
            self.callback, self.every, self.closed = callback, every, False
            intervals.append(self)

        def start(self):
            assert first.take("one", "claude") == "a"

        def close(self):
            self.closed = True

    monkeypatch.setattr(account_slots, "Interval", Interval)

    def launch(account):
        now[0] += account_slots.TTL_MS - 1
        intervals[0].callback()
        now[0] += 2
        assert second.take("two", "claude") is None
        return account

    assert first.launch("one", "claude", "", launch) == "a"
    assert intervals[0].every == account_slots.presence.BEAT_SECONDS
    assert intervals[0].closed is True


def test_an_ambiguous_launch_retains_its_slot(rig):
    from scripts.swarm.tick import SpawnError

    first, second, _, _, _ = rig

    def launch(account):
        raise SpawnError("uncertain", "ambiguous")

    with pytest.raises(SpawnError, match="^uncertain$"):
        first.launch("one", "claude", "", launch)
    assert second.take("two", "claude") is None


def test_contended_state_retries_are_bounded_and_fail_admission_closed(rig, monkeypatch):
    from scripts.events.bus import StateConflict
    from scripts.hive import account_slots

    first, _, _, _, bus = rig
    state = bus.state("hive-slot-accounts")
    calls = []

    def conflict(*args):
        calls.append(args)
        raise StateConflict

    monkeypatch.setattr(state, "create", conflict)
    with pytest.raises(account_slots.Unavailable, match="^Account slot admission failed$"):
        first.launch("one", "claude", "", lambda account: None)
    assert len(calls) == 4


@pytest.mark.parametrize(
    "route,account", [([], "a"), (["--route", "a"], "a"), (["--route", "interactive"], "unrouted")]
)
def test_runtime_launch_pins_the_reserved_route_and_closes_the_bus(rig, monkeypatch, route, account):
    from scripts.events import factory
    from scripts.hive import account_slots, daemon

    _, _, _, _, bus = rig
    report = reading()
    report["slots"][0]["account"] = account
    monkeypatch.setattr(daemon, "observe", lambda: report)
    monkeypatch.setattr(factory, "open_bus", lambda config: bus)
    closed = []
    monkeypatch.setattr(bus, "close", lambda: closed.append(True))
    argv = ["launcher", "--agent", "claude", "--dir", "repo", "--", *route, "--model", "model"]
    expected_route = "interactive" if account == "unrouted" else account
    result = account_slots.launch("one", "execution", argv, lambda args: args)
    assert result == [
        "launcher",
        "--agent",
        "claude",
        "--dir",
        "repo",
        "--",
        "--route",
        expected_route,
        "--model",
        "model",
    ]
    assert closed == [True]


def test_distributed_heartbeat_publishes_even_without_any_executions(rig, monkeypatch):
    from scripts.events import factory
    from scripts.events.config import BusConfig
    from scripts.hive import account_slots

    _, second, now, _, bus = rig
    monkeypatch.setattr(BusConfig, "from_environ", lambda: BusConfig(backend="nats"))
    monkeypatch.setattr(factory, "open_bus", lambda config: bus)
    monkeypatch.setattr(account_slots.presence, "now_ms", lambda: now[0])
    monkeypatch.setattr(bus, "close", lambda: None)
    account_slots.publish("one", reading(sessions=1), {"executions": {}})
    assert second.take("two", "claude") is None


def test_pending_launch_renewal_does_not_hide_a_manual_session(rig, monkeypatch):
    from scripts.hive import account_slots

    first, second, _, report, _ = rig
    report[0] = reading(cap=2, sessions=1)
    second.read = lambda: reading(cap=2)

    class Interval:
        def __init__(self, callback, **kwargs):
            self.callback = callback

        def start(self):
            self.callback()

        def close(self):
            pass

    monkeypatch.setattr(account_slots, "Interval", Interval)

    def launch(account):
        assert second.take("second", "claude") is None
        return account

    assert first.launch("first", "claude", "", launch) == "a"
