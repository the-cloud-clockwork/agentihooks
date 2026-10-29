import json

import pytest

from scripts.events.config import BusConfig
from scripts.events.memory import open_bus
from scripts.hub import slots


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
    slots.serve(bus, clock=lambda: now[0])
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
    first.heartbeat(
        {**reading(cap=3, sessions=2), "sessions": {"claude:a": 2, "_executions": {"claude:a": ["one"]}}},
        {"one": {"pane": {"state": "working"}}},
    )
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


def test_the_launch_heartbeat_keeps_the_slot_occupied_during_a_blocking_start(rig):
    from scripts.hive import account_slots

    first, second, now, _, _ = rig
    assert first.launch("one", "claude", "", lambda account: account) == "a"
    now[0] += account_slots.TTL_MS - 1
    first.heartbeat(reading(), {"one": {"pane": {"state": "working"}}})
    now[0] += 2
    assert second.take("two", "claude") is None


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


@pytest.mark.parametrize("account", ["a", "unrouted", "resolved-login"])
def test_runtime_launch_pins_the_reserved_route_and_closes_the_bus(rig, monkeypatch, account):
    from scripts.events import factory
    from scripts.hive import account_slots

    _, _, _, _, bus = rig
    report = reading()
    report["slots"][0]["account"] = account
    report["slots"].insert(0, {"harness": "claude", "account": "different", "cap": 2})

    def local_reading(hive):
        assert hive == "one"
        return report

    monkeypatch.setattr(account_slots, "_reading", local_reading)

    def opened(config):
        assert isinstance(config, BusConfig)
        return bus

    monkeypatch.setattr(factory, "open_bus", opened)
    closed = []
    monkeypatch.setattr(bus, "close", lambda: closed.append(True))
    env = {account_slots.HIVE_ENV: "one", account_slots.EXECUTION_ENV: "execution", "KEEP": "yes"}
    result = account_slots.execute("claude", account, env, lambda child: child)
    assert result == {"KEEP": "yes"}
    assert closed == [True]
    entry = bus.state("account-slots").get(account_slots.key("claude", account))
    assert json.loads(entry.value)["holders"]["execution"]["hive"] == "one"


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


def test_pending_launch_renewal_does_not_hide_a_manual_session(rig):
    first, second, _, report, _ = rig
    report[0] = reading(cap=2, sessions=1)
    second.read = lambda: reading(cap=2)

    def launch(account):
        first.heartbeat(report[0], {"first": {"pane": {"state": "working"}}})
        assert second.take("second", "claude") is None
        return account

    assert first.launch("first", "claude", "", launch) == "a"


def test_local_reading_correlates_the_same_process_sample_to_its_own_hive(monkeypatch):
    from hooks.context import account_sessions
    from scripts.hive import account_slots, daemon
    from scripts.swarm import store

    processes = {"claude": {10: "a"}, "codex": {}}
    report = reading()
    seen = []
    connection = object()
    monkeypatch.setattr(account_sessions, "local_processes", lambda: processes)
    monkeypatch.setattr(daemon, "observe", lambda **kwargs: (seen.append(kwargs), report)[1])
    monkeypatch.setattr(store, "connect", lambda: connection)

    def executions(redis, hive, sample):
        assert redis is connection
        assert hive == "one"
        assert sample is processes
        return {"claude:a": ["execution"]}

    monkeypatch.setattr(daemon, "_executions", executions)
    assert account_slots._reading("one") == {
        **reading(),
        "sessions": {"claude:a": 0, "_executions": {"claude:a": ["execution"]}},
    }
    assert seen == [{"processes": processes}]


def test_an_unmanaged_exec_keeps_its_environment_without_opening_the_bus(monkeypatch):
    from scripts.events import factory
    from scripts.hive import account_slots

    def refused(*args):
        pytest.fail("unmanaged exec opened a bus")

    monkeypatch.setattr(factory, "open_bus", refused)
    assert account_slots.execute("claude", "a", {"KEEP": "yes"}, lambda child: child) == {"KEEP": "yes"}


def test_exec_failure_returns_capacity_and_closes_the_connection(rig, monkeypatch):
    from scripts.events import factory
    from scripts.hive import account_slots

    _, second, _, _, bus = rig
    monkeypatch.setattr(account_slots, "_reading", lambda hive: reading())
    monkeypatch.setattr(factory, "open_bus", lambda config: bus)
    closed = []
    monkeypatch.setattr(bus, "close", lambda: closed.append(True))

    def failed(child):
        assert child == {"KEEP": "yes"}
        assert second.take("other", "claude") is None
        raise OSError("exec refused")

    env = {account_slots.HIVE_ENV: "one", account_slots.EXECUTION_ENV: "execution", "KEEP": "yes"}
    with pytest.raises(OSError, match="^exec refused$"):
        account_slots.execute("claude", "a", env, failed)
    assert second.take("other", "claude") == "a"
    assert closed == [True]


def test_release_is_idempotent_before_reservation_and_for_unknown_executions(rig):
    first, second, _, _, _ = rig
    first.release("never", "claude", "a")
    assert first.take("one", "claude") == "a"
    first.release("unknown", "claude", "a")
    assert second.take("two", "claude") is None
    first.release("one", "claude", "a")
    first.release("one", "claude", "a")
    assert second.take("two", "claude") == "a"


def test_account_keys_keep_harnesses_accounts_and_hive_indexes_separate(rig):
    from scripts.hive import account_slots

    first, _, _, _, bus = rig
    keys = {
        account_slots.key(harness, account) for harness in ("claude", "codex", "hive") for account in ("one", "two")
    }
    assert len(keys) == 6
    assert first.take("one", "claude") == "a"
    entry = bus.state("hive-slot-accounts").get(account_slots.key("hive", "one"))
    assert json.loads(entry.value) == {"accounts": [["claude", "a"]]}


def test_launch_respects_a_preferred_account_even_when_another_is_first(rig):
    first, _, _, report, _ = rig
    report[0]["slots"].insert(0, {"harness": "claude", "account": "b", "cap": 2})
    assert first.launch("one", "claude", "a", lambda account: account) == "a"


def test_foreign_process_correlation_never_hides_our_manual_sessions(rig):
    first, second, _, report, _ = rig
    second.read = lambda: reading(cap=2)
    assert second.take("other", "claude") == "a"
    report[0] = {**reading(cap=2, sessions=1), "sessions": {"claude:a": 1, "_executions": {"claude:a": ["other"]}}}
    first.heartbeat(report[0], {"other": {"pane": {"state": "working"}}})
    assert first.take("one", "claude") is None


def test_missing_quota_keeps_live_ownership_closed_until_a_fresh_report(rig):
    from scripts.hive import account_slots

    first, second, _, _, bus = rig
    assert first.take("one", "claude") == "a"
    first.heartbeat({"slots": [], "sessions": {}}, {"one": {"pane": {"state": "working"}}})
    value = json.loads(bus.state("account-slots").get(account_slots.key("claude", "a")).value)
    assert value["hives"]["one"]["cap"] == 0
    first.release("one", "claude", "a")
    assert second.take("two", "claude") is None
    first.heartbeat({"slots": [], "sessions": {}}, {})
    first.heartbeat({"slots": [], "sessions": {}}, {})
    assert second.take("two", "claude") == "a"


def test_memory_heartbeat_never_opens_a_new_independent_bus(monkeypatch):
    from scripts.events import factory
    from scripts.events.config import BusConfig
    from scripts.hive import account_slots

    monkeypatch.setattr(BusConfig, "from_environ", lambda: BusConfig())
    monkeypatch.setattr(factory, "open_bus", lambda config: pytest.fail("memory heartbeat opened a bus"))
    account_slots.publish("one", reading(), {"executions": {}})


def test_published_exit_uses_the_current_hive_and_real_execution_report(rig, monkeypatch):
    from scripts.events import factory
    from scripts.events.config import BusConfig
    from scripts.hive import account_slots

    first, second, now, _, bus = rig
    assert first.take("one", "claude") == "a"
    monkeypatch.setattr(BusConfig, "from_environ", lambda: BusConfig(backend="nats"))
    closed = []

    def opened(config):
        assert config.backend == "nats"
        return bus

    monkeypatch.setattr(factory, "open_bus", opened)
    monkeypatch.setattr(bus, "close", lambda: closed.append(True))
    account_slots.publish("one", reading(), {"executions": {"one": {"pane": {"state": "exited"}}}})
    assert second.take("two", "claude") == "a"
    assert closed == [True]


def test_public_release_returns_the_requested_hive_execution_and_closes_the_bus(rig, monkeypatch):
    from scripts.events import factory
    from scripts.events.config import SWARMS_ENV, BusConfig
    from scripts.hive import account_slots

    first, second, _, _, bus = rig
    assert first.take("one", "claude") == "a"
    closed = []
    recording = Recording(bus, closed)
    monkeypatch.setattr(BusConfig, "from_environ", lambda: BusConfig(backend="nats"))
    monkeypatch.setattr(factory, "open_bus", lambda config: recording)
    monkeypatch.setenv(SWARMS_ENV, "rig,other")
    account_slots.release("one", "one", "claude", "a")
    assert second.take("two", "claude") == "a"
    assert closed == [True]
    assert recording.sent == [
        (
            "slots.rig.one",
            {"op": "release", "hive": "one", "execution": "one", "harness": "claude", "account": "a"},
            account_slots.REQUEST_TIMEOUT_MS,
        )
    ]


class Recording:
    def __init__(self, bus, closed):
        self.bus, self.closed, self.sent = bus, closed, []

    def request(self, subject, data, timeout_ms):
        self.sent.append((subject, json.loads(data), timeout_ms))
        return self.bus.request(subject, data, timeout_ms)

    def close(self):
        self.closed.append(True)


def test_memory_release_opens_no_bus(monkeypatch):
    from scripts.events import factory
    from scripts.events.config import BusConfig
    from scripts.hive import account_slots

    monkeypatch.setattr(BusConfig, "from_environ", lambda: BusConfig())
    monkeypatch.setattr(factory, "open_bus", lambda config: pytest.fail("memory release opened a bus"))
    account_slots.release("one", "one", "claude", "a")


@pytest.mark.parametrize(
    ("environ", "swarm"),
    [({"AGENTIHOOKS_BUS_SWARMS": "rig,other"}, "rig"), ({"AGENTIHOOKS_BUS_SWARMS": ",rig"}, "rig"), ({}, "hive")],
)
def test_a_member_asks_on_its_first_configured_swarm(environ, swarm):
    from scripts.hive import account_slots

    assert account_slots._swarm(environ) == swarm


def test_a_remote_take_sends_the_whole_reservation_with_a_fresh_reading():
    from scripts.hive import account_slots

    sent = []
    remote = account_slots.Remote(lambda request: sent.append(request) or "a", "one", lambda: reading(cap=2))
    assert remote.take("execution", "claude", "a") == "a"
    assert sent == [
        {
            "op": "take",
            "hive": "one",
            "execution": "execution",
            "harness": "claude",
            "preferred": "a",
            "report": reading(cap=2),
        }
    ]


def test_only_the_memory_backend_reserves_in_local_state(rig):
    from scripts.hive import account_slots

    *_, bus = rig

    def read():
        return reading()

    local = account_slots._slots(bus, "memory", "one", read, {})
    assert type(local) is account_slots.Slots
    assert (local.bus, local.hive, local.read) == (bus, "one", read)
    remote = account_slots._slots(bus, "nats", "one", read, {"AGENTIHOOKS_BUS_SWARMS": "rig"})
    assert type(remote) is account_slots.Remote
    assert (remote.hive, remote.read) == ("one", read)
    assert remote.take("execution", "claude") == "a"


def test_a_memory_launch_reserves_in_the_opened_bus_without_a_hub(monkeypatch):
    from scripts.events import factory
    from scripts.events.config import BusConfig
    from scripts.hive import account_slots

    bus = open_bus(BusConfig())
    monkeypatch.setattr(BusConfig, "from_environ", lambda: BusConfig())
    monkeypatch.setattr(factory, "open_bus", lambda config: bus)
    monkeypatch.setattr(account_slots, "_reading", lambda hive: reading())
    monkeypatch.setattr(bus, "close", lambda: None)
    env = {account_slots.HIVE_ENV: "one", account_slots.EXECUTION_ENV: "execution"}
    assert account_slots.execute("claude", "a", env, lambda child: child) == {}
    entry = bus.state("account-slots").get(account_slots.key("claude", "a"))
    assert json.loads(entry.value)["holders"]["execution"]["hive"] == "one"
    bus.close()


def test_a_member_launch_asks_the_hub_on_the_swarm_its_launch_names(rig, monkeypatch):
    from scripts.events import factory
    from scripts.events.config import SWARMS_ENV, BusConfig
    from scripts.hive import account_slots

    *_, bus = rig
    recording = Recording(bus, [])
    monkeypatch.setattr(BusConfig, "from_environ", lambda: BusConfig(backend="nats"))
    monkeypatch.setattr(factory, "open_bus", lambda config: recording)
    monkeypatch.setattr(account_slots, "_reading", lambda hive: reading())
    env = {account_slots.HIVE_ENV: "one", account_slots.EXECUTION_ENV: "execution", SWARMS_ENV: "rig"}
    assert account_slots.execute("claude", "a", env, lambda child: child) == {SWARMS_ENV: "rig"}
    assert {subject for subject, _, _ in recording.sent} == {"slots.rig.one"}


def test_two_real_nats_clients_share_atomic_account_capacity(tmp_path):
    from scripts.events import nats
    from scripts.events.config import BusConfig
    from scripts.hive import account_slots
    from tests.events import nats_server

    with nats_server.running(tmp_path / "nats") as url:
        config = BusConfig(backend="nats", url=url)
        first_bus, second_bus = nats.open_bus(config), nats.open_bus(config)
        try:
            first = account_slots.Slots(first_bus, "one", reading, lambda: 1000)
            second = account_slots.Slots(second_bus, "two", reading, lambda: 1000)
            assert first.take("first", "claude") == "a"
            assert second.take("second", "claude") is None
            first.release("first", "claude", "a")
            assert second.take("second", "claude") == "a"
        finally:
            first_bus.close()
            second_bus.close()
