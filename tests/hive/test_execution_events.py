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
