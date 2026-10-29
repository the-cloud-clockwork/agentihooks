from unittest.mock import patch

import pytest

from scripts.events.config import BusConfig
from scripts.events.memory import open_bus


def report(state):
    return {
        "observed_at": 1234,
        "executions": {
            "execution-a": {
                "slug": "rig",
                "name": "agent-a",
                "generation": 7,
                "pane": {"state": state},
            }
        },
    }


def test_hive_publishes_started_then_exited_once_with_execution_identity():
    from scripts.hive.execution_events import publish

    bus = open_bus(BusConfig())
    try:
        publish(bus, "hive-a", report("working"))
        publish(bus, "hive-a", report("idle"))
        publish(bus, "hive-a", report("exited"))
        publish(bus, "hive-a", report("exited"))
        events = bus.events.consumer("audit", "swarm.rig.execution.*").fetch(10, 0)
        assert [item.event.type for item in events] == ["execution.started", "execution.exited"]
        for item in events:
            event = item.event
            assert (event.key, event.swarm, event.version, event.author, event.time) == (
                "execution-a",
                "rig",
                7,
                "hive-a",
                1234,
            )
            assert event.data == {"name": "agent-a", "hive": "hive-a", "generation": 7}
    finally:
        bus.close()


@pytest.mark.parametrize("state", ["unknown", "", "waiting", "blocked"])
def test_uncertain_hive_observation_never_publishes_an_exit(state):
    from scripts.hive.execution_events import publish

    bus = open_bus(BusConfig())
    try:
        publish(bus, "hive-a", report(state))
        assert bus.events.consumer("audit", "swarm.rig.execution.exited").fetch(1, 0) == []
    finally:
        bus.close()


def test_failed_exit_publication_can_be_retried_after_publisher_restart():
    from scripts.hive.execution_events import publish

    bus = open_bus(BusConfig())
    try:
        publish(bus, "hive-a", report("working"))
        with patch.object(bus.events, "publish", side_effect=RuntimeError("offline")):
            with pytest.raises(RuntimeError, match="offline"):
                publish(bus, "hive-a", report("exited"))
        publish(bus, "hive-a", report("exited"))
        assert len(bus.events.consumer("audit", "swarm.rig.execution.exited").fetch(10, 0)) == 1
    finally:
        bus.close()


@pytest.mark.parametrize("state", ["dead", "exited"])
def test_a_confirmed_exit_remains_terminal_after_late_live_observations(state):
    from scripts.hive.execution_events import publish

    bus = open_bus(BusConfig())
    publish(bus, "hive-a", report(state))
    publish(bus, "hive-a", report("working"))
    events = bus.events.consumer("audit", "swarm.rig.execution.*").fetch(10, 0)
    assert [item.event.type for item in events] == ["execution.exited"]
    assert events[0].event.cause.startswith("started:")
    bus.close()


def test_bus_failure_defers_publication_and_a_later_observation_retries(monkeypatch):
    from scripts.hive import execution_events

    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    bus = open_bus(BusConfig())
    with patch.object(execution_events, "open_bus", side_effect=RuntimeError("offline")):
        assert execution_events.observe("hive-a", report("working")) is None
    with (
        patch.object(execution_events, "open_bus", return_value=bus),
        patch.object(bus, "close") as close,
    ):
        execution_events.observe("hive-a", report("working"))
    close.assert_called_once_with()
    assert len(bus.events.consumer("audit", "swarm.rig.execution.started").fetch(10, 0)) == 1
    bus.close()


def test_unconfigured_or_empty_hive_observation_needs_no_bus(monkeypatch):
    from scripts.hive import execution_events

    with patch.object(execution_events, "open_bus", side_effect=AssertionError("unneeded")):
        monkeypatch.setenv("AGENTIHOOKS_BUS", "memory")
        execution_events.observe("hive-a", report("working"))
        monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
        execution_events.observe("hive-a", {"executions": {}})


def test_a_reused_agent_name_never_inherits_an_older_generation_envelope():
    from scripts.events.envelope import Envelope
    from scripts.hive.execution_events import publish, started

    bus = open_bus(BusConfig())
    first = Envelope(
        "first-start",
        "execution.started",
        "rig",
        "execution",
        "first-execution",
        6,
        123,
        "hive-a",
        {"name": "agent-a", "claim_key": "first-claim", "claim_revision": 2},
    )
    started(bus, first)
    publish(bus, "hive-a", report("dead"))
    events = bus.events.consumer("audit", "swarm.rig.execution.*").fetch(10, 0)
    assert [(item.event.key, item.event.version) for item in events] == [("first-execution", 6), ("execution-a", 7)]
    assert "claim_key" not in events[1].event.data
    bus.close()


def test_registered_start_is_reused_and_updated_without_a_pending_marker():
    from dataclasses import replace

    from scripts.hive.execution_events import initial, started

    bus = open_bus(BusConfig())
    record = report("working")["executions"]["execution-a"]
    event = initial(bus, "hive-a", "execution-a", record, 111)
    started(bus, event)
    assert initial(bus, "hive-a", "execution-a", record, 222) == event
    updated = replace(event, time=333)
    started(bus, updated)
    started(bus, updated)
    assert initial(bus, "hive-a", "execution-a", record, 444) == updated
    assert len(bus.events.consumer("audit", "swarm.rig.execution.started").fetch(10, 0)) == 1
    bus.close()


def test_initial_identity_and_exit_time_follow_each_report():
    import hashlib
    import json

    from scripts.hive.execution_events import STATE, initial, publish, started

    bus = open_bus(BusConfig())
    live = report("working")
    event = initial(bus, "hive-a", "execution-a", live["executions"]["execution-a"], live["observed_at"])
    started(bus, event)
    receipt = bus.state(STATE).get(hashlib.sha256(event.id.encode()).hexdigest())
    assert receipt.value == b"sent"
    ended = report("exited")
    ended["observed_at"] = 4321
    publish(bus, "hive-a", ended)
    events = [item.event for item in bus.events.consumer("audit", "swarm.rig.execution.*").fetch(10, 0)]
    key = hashlib.sha256(json.dumps(["hive-a", "execution-a", 7]).encode()).hexdigest()
    assert events[0].id == f"started:{key}:7"
    assert events[0].entity == "execution"
    assert events[1].time == 4321
    bus.close()


@pytest.mark.parametrize("pending", [False, True])
def test_one_unready_execution_does_not_hide_other_execution_events(pending):
    from scripts.hive.execution_events import prepare, publish

    bus = open_bus(BusConfig())
    first = report("working" if pending else "unknown")
    if pending:
        prepare(bus, "rig", "agent-a", 7)
    first["executions"]["execution-b"] = {
        "slug": "rig",
        "name": "agent-b",
        "generation": 8,
        "pane": {"state": "working"},
    }
    publish(bus, "hive-a", first)
    events = bus.events.consumer("audit", "swarm.rig.execution.started").fetch(10, 0)
    assert [item.event.key for item in events] == ["execution-b"]
    bus.close()


@pytest.mark.parametrize("backend,executions", [("memory", True), ("nats", False)])
def test_observation_skips_unconfigured_bus_and_empty_reports(monkeypatch, backend, executions):
    from scripts.hive import execution_events

    monkeypatch.setenv("AGENTIHOOKS_BUS", backend)
    sample = report("working") if executions else {"executions": {}}
    with patch.object(execution_events, "open_bus") as connect:
        execution_events.observe("hive-a", sample)
    connect.assert_not_called()


def test_observation_uses_configured_authority_and_preserves_failure_diagnostics(monkeypatch, caplog):
    from scripts.hive import execution_events

    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.setenv("AGENTIHOOKS_BUS_URL", "nats://hive.invalid:4222")
    config = BusConfig.from_environ()
    bus = open_bus(BusConfig())
    sample = report("working")
    with (
        patch.object(execution_events, "open_bus", return_value=bus) as connect,
        patch.object(execution_events, "publish", side_effect=RuntimeError("offline")) as publish,
        patch.object(bus, "close") as close,
    ):
        execution_events.observe("hive-a", sample)
    connect.assert_called_once_with(config)
    publish.assert_called_once_with(bus, "hive-a", sample)
    close.assert_called_once_with()
    assert caplog.messages == ["Execution events deferred: RuntimeError"]
    bus.close()
