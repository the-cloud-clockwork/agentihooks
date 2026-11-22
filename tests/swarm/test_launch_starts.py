import argparse
import json
from collections.abc import Iterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, call

import pytest

pytestmark = pytest.mark.feature("swarm-launch-acknowledgements")


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    from scripts.events import factory, memory
    from scripts.events.config import BusConfig
    from scripts.events.envelope import Envelope
    from scripts.hive import execution_events, execution_stop
    from scripts.swarm.store import AgentRecord

    bus: Any = memory.open_bus(BusConfig())
    agent = AgentRecord(name="engineer", lane="eng", task="task", execution_id="execution", generation=3)
    missing = replace(agent, name="missing", execution_id="not-started")
    store = SimpleNamespace(agents=Mock(return_value=[missing, agent]))
    event = Envelope(
        id="acknowledged",
        type="execution.started",
        swarm="rig",
        entity="execution",
        key="execution",
        version=3,
        time=950_000,
        author="hive",
        data={"name": "engineer", "task": "task", "hive": "hive", "claim_key": "claim", "launched_at": 900_000},
    )
    execution_events.started(bus, event)
    opened = Mock(return_value=bus)
    closed = Mock(wraps=bus.close)
    reader = Mock(wraps=execution_stop.started)
    monkeypatch.setattr(factory, "open_bus", opened)
    monkeypatch.setattr(bus, "close", closed)
    monkeypatch.setattr(execution_stop, "started", reader)
    yield SimpleNamespace(
        bus=bus, agent=agent, missing=missing, store=store, event=event, opened=opened, closed=closed, reader=reader
    )
    bus.close()


@pytest.mark.feature("swarm-launch-acknowledgements")
def test_launches_parser_dispatches_the_public_command() -> None:
    from scripts.swarm import cli, launch_starts

    args = cli.build_parser().parse_args(["rig", "launches"])

    assert args.handler is launch_starts.command
    assert args.slug == "rig"


@pytest.mark.feature("swarm-launch-acknowledgements")
def test_launches_print_acknowledged_and_missing_starts(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.events.config import BusConfig
    from scripts.swarm import launch_starts

    launch_starts.command(rig.store, argparse.Namespace(slug="rig"))

    assert json.loads(capsys.readouterr().out) == [
        {"agent": "engineer", "hive": "hive", "launched_at": 900_000},
        {"agent": "missing", "hive": None, "launched_at": None},
    ]
    rig.store.agents.assert_called_once_with("rig")
    rig.opened.assert_called_once_with(BusConfig.from_environ())
    assert rig.reader.call_args_list == [call(rig.bus, "rig", "engineer", 3), call(rig.bus, "rig", "missing", 3)]
    rig.closed.assert_called_once_with()


@pytest.mark.feature("swarm-launch-acknowledgements")
def test_launches_print_reconstructed_acknowledgement_time(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.hive import execution_events
    from scripts.swarm import launch_starts

    data = dict(rig.event.data)
    del data["launched_at"]
    execution_events.started(rig.bus, replace(rig.event, id="rebuilt", data=data))
    rig.store.agents.return_value = [rig.agent]

    launch_starts.command(rig.store, argparse.Namespace(slug="rig"))

    assert json.loads(capsys.readouterr().out) == [{"agent": "engineer", "hive": "hive", "launched_at": 950_000}]


@pytest.mark.feature("swarm-launch-acknowledgements")
@pytest.mark.parametrize("value", [None, b"null"])
def test_launches_print_missing_acknowledgement(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str], value: bytes | None
) -> None:
    from scripts.hive import execution_events
    from scripts.swarm import launch_starts

    agent = replace(rig.agent, generation=4)
    if value is not None:
        starts = rig.bus.for_swarm("rig").state(execution_events.STARTS)
        starts.create(execution_events.identity("rig", "engineer", 4), value)
    rig.store.agents.return_value = [agent]

    launch_starts.command(rig.store, argparse.Namespace(slug="rig"))

    assert json.loads(capsys.readouterr().out) == [{"agent": "engineer", "hive": None, "launched_at": None}]


@pytest.mark.feature("swarm-launch-acknowledgements")
@pytest.mark.parametrize(
    "changes",
    [
        {"type": "execution.exited"},
        {"swarm": "other"},
        {"key": "other"},
        {"version": 4},
        {"data": {"name": "other", "task": "task", "claim_key": "claim"}},
        {"data": {"name": "engineer", "task": "other", "claim_key": "claim"}},
        {"data": {"name": "engineer", "task": "task"}},
    ],
)
def test_launches_reject_mismatched_acknowledgements(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str], changes: dict[str, object]
) -> None:
    from scripts.hive import execution_events
    from scripts.swarm import launch_starts

    starts = rig.bus.for_swarm("rig").state(execution_events.STARTS)
    key = execution_events.identity("rig", "engineer", 3)
    entry = starts.get(key)
    starts.update(key, replace(rig.event, **changes).encode(), entry.revision)
    rig.store.agents.return_value = [rig.agent]

    launch_starts.command(rig.store, argparse.Namespace(slug="rig"))

    assert json.loads(capsys.readouterr().out) == [{"agent": "engineer", "hive": None, "launched_at": None}]


@pytest.mark.feature("swarm-launch-acknowledgements")
def test_launches_close_the_bus_on_read_failure(rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.swarm import launch_starts

    rig.reader.side_effect = RuntimeError("start unavailable")

    with pytest.raises(RuntimeError, match="^start unavailable$"):
        launch_starts.command(rig.store, argparse.Namespace(slug="rig"))

    assert capsys.readouterr().out == ""
    rig.closed.assert_called_once_with()


@pytest.mark.feature("swarm-launch-acknowledgements")
def test_launches_print_an_empty_agent_list(rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.swarm import launch_starts

    rig.store.agents.return_value = []

    launch_starts.command(rig.store, argparse.Namespace(slug="rig"))

    assert json.loads(capsys.readouterr().out) == []
    rig.reader.assert_not_called()
    rig.closed.assert_called_once_with()
