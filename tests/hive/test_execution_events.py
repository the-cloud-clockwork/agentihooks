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
