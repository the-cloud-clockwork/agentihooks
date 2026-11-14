import json
from collections.abc import Iterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.events.config import BusConfig
from scripts.events.memory import open_bus
from scripts.hive import execution_events
from scripts.hub import exits
from scripts.hub.lifecycle import Lifecycle
from scripts.swarm.store import SwarmError


class Registry:
    def __init__(self, owners: dict[str, str]) -> None:
        self.owners = owners

    def get(self, swarm: str, execution: str) -> SimpleNamespace:
        if execution not in self.owners:
            raise SwarmError(f"no execution {execution}")
        return SimpleNamespace(hive=self.owners[execution])


def report(state: str) -> dict:
    return {
        "observed_at": 1234,
        "executions": {"execution-a": {"slug": "rig", "name": "agent-a", "generation": 7, "pane": {"state": state}}},
    }


@pytest.fixture
def hub() -> Iterator[Any]:
    bus = open_bus(BusConfig())
    store = SimpleNamespace(execution_registry=Registry({"execution-a": "hive-a"}))
    exits.serve(bus, lambda: store)
    yield bus
    bus.close()


def shared(bus: Any) -> list[Any]:
    return [item.event for item in bus.events.consumer("audit", Lifecycle(bus).reactor().subject).fetch(10, 0)]


@pytest.mark.feature("hive-own-exit-subject")
def test_a_hive_exit_reaches_the_lifecycle_subject_through_the_hub(hub: Any) -> None:
    execution_events.publish(hub, "hive-a", report("working"))
    execution_events.publish(hub, "hive-a", report("exited"))
    execution_events.publish(hub, "hive-a", report("exited"))

    assert [(event.type, event.key, event.author) for event in shared(hub)] == [
        ("execution.exited", "execution-a", "hive-a")
    ]


@pytest.mark.feature("hive-own-exit-subject")
def test_the_hub_refuses_an_exit_reported_by_another_hive(hub: Any) -> None:
    execution_events.publish(hub, "hive-b", report("working"))

    with pytest.raises(execution_events.ExitRefused) as refused:
        execution_events.publish(hub, "hive-b", report("exited"))

    assert (
        str(refused.value) == "hive 'hive-b' reported an exit for execution 'execution-a', which runs on hive 'hive-a'"
    )
    assert shared(hub) == []


@pytest.mark.feature("hive-own-exit-subject")
def test_a_hive_on_a_swarm_scoped_bus_reports_its_exit_through_the_hub() -> None:
    bus = open_bus(BusConfig(scoped=True))
    exits.serve(bus, lambda: SimpleNamespace(execution_registry=Registry({"execution-a": "hive-a"})))
    hive = bus.for_swarm("rig")
    event = execution_events.initial(hive, "hive-a", "execution-a", report("exited")["executions"]["execution-a"], 1)
    assert event is not None

    execution_events.report_exit(hive, "hive-a", replace(event, id=f"exited:{event.id}", type="execution.exited"))

    found = hive.events.consumer("audit", "swarm.rig.execution.exited").fetch(10, 0)
    assert [(item.event.type, item.event.key) for item in found] == [("execution.exited", "execution-a")]
    bus.close()


@pytest.mark.feature("hive-own-exit-subject")
def test_an_exit_for_an_execution_the_hub_no_longer_holds_is_settled_unpublished(hub: Any) -> None:
    event = execution_events.initial(hub, "hive-a", "execution-z", report("exited")["executions"]["execution-a"], 1)
    assert event is not None
    exited = replace(event, key="execution-z", type="execution.exited")

    answer = exits.handle(
        hub,
        lambda: SimpleNamespace(execution_registry=Registry({})),
        "exits.rig.hive-a",
        json.dumps(exited.to_dict()).encode(),
    )

    assert json.loads(answer) == {"ok": True, "result": False}
    assert shared(hub) == []


@pytest.mark.feature("hive-own-exit-subject")
@pytest.mark.parametrize(
    ("reported", "change", "error"),
    [
        ("exits.rig", {}, "not an exit report subject: 'exits.rig'"),
        ("slots.rig.hive-a", {}, "not an exit report subject: 'slots.rig.hive-a'"),
        ("exits.other.hive-a", {}, "exit report on 'exits.other.hive-a' carries 'execution.exited' for swarm 'rig'"),
        (
            "exits.rig.hive-a",
            {"type": "execution.started"},
            "exit report on 'exits.rig.hive-a' carries 'execution.started' for swarm 'rig'",
        ),
    ],
)
def test_the_hub_refuses_a_malformed_exit_report(hub: Any, reported: str, change: dict, error: str) -> None:
    event = execution_events.initial(hub, "hive-a", "execution-a", report("exited")["executions"]["execution-a"], 1)
    assert event is not None
    data = {**event.to_dict(), "type": "execution.exited", **change}

    answer = exits.handle(
        hub,
        lambda: SimpleNamespace(execution_registry=Registry({"execution-a": "hive-a"})),
        reported,
        json.dumps(data).encode(),
    )

    assert json.loads(answer) == {"ok": False, "error": error}
    assert shared(hub) == []
