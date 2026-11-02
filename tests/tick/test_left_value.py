"""A tick step held or left running past its budget leaves its pass value unset, and the pass still completes."""

from collections.abc import Callable, Iterator
from threading import Event
from typing import Any

import pytest

import scripts.swarm.cli  # noqa: F401
from scripts.tick import engine
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
from scripts.tick.steps import PASS, CallStep, Left, PassBox, host, observe

pytestmark = pytest.mark.unit

GROUP = "left value"


class _State:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state

    def read(self, domain: Domain, key: str) -> object | None:
        return self.state.get(key) if domain == PASS else None


def _ctx(box: dict[str, Any]) -> Context:
    return Context(_State({"box": box}), Deadline.after(600.0))


def _steps(release: Event, read: list[object]) -> tuple[CallStep, CallStep]:
    def write(cli: Any, get: Callable[[str], Any]) -> list[str]:
        release.wait(300.0)
        get("box")["found"] = ["finding"]
        return []

    def use(cli: Any, get: Callable[[str], Any]) -> list[str]:
        read.append(get("box")["found"])
        return ["used"]

    writer = CallStep("health findings", GROUP, host.held(write))
    writer.budget_s = 0.1
    reader = CallStep("findings pass", GROUP, host.held(use), frozenset({"health findings"}))
    return writer, reader


@pytest.fixture
def release() -> Iterator[Event]:
    event = Event()
    yield event
    event.set()
    running = engine._RUNNING.get("health findings")
    if running is not None:
        running.join(30.0)


@pytest.mark.feature("swarm-tick-left-pass-value")
def test_a_writer_left_past_its_budget_skips_its_reader_and_the_pass_completes(release: Event) -> None:
    read: list[object] = []
    box: dict[str, Any] = PassBox()
    records = engine.run_group(GROUP, _ctx(box), _steps(release, read))
    assert [record.outcome for record in records] == [engine.LATE, engine.OK]
    assert "raised" not in box
    assert read == []
    host.raise_held(box)


@pytest.mark.feature("swarm-tick-left-pass-value")
def test_a_held_writer_skips_its_reader_and_the_pass_completes(release: Event) -> None:
    read: list[object] = []
    writer, reader = _steps(release, read)
    engine.run_step(writer, _ctx(PassBox()))
    box: dict[str, Any] = PassBox()
    records = engine.run_group(GROUP, _ctx(box), (writer, reader))
    assert [record.outcome for record in records] == [engine.HELD, engine.OK]
    assert "raised" not in box
    assert read == []
    host.raise_held(box)


@pytest.mark.feature("swarm-tick-left-pass-value")
def test_an_observe_step_missing_a_pass_value_is_skipped_without_holding_an_error() -> None:
    def needs_config(config: object) -> list[str]:
        return ["ran"]

    box: dict[str, Any] = PassBox()
    tick = type("Tick", (), {"step": staticmethod(needs_config)})
    found = observe._run(lambda module: module.step, tick, lambda key: box if key == "box" else None)
    assert found == []
    assert "raised" not in box
    observe.raise_held(box)


@pytest.mark.feature("swarm-tick-left-pass-value")
def test_a_pass_box_names_the_left_value_as_a_key_error() -> None:
    with pytest.raises(Left) as raised:
        PassBox()["found"]
    assert isinstance(raised.value, KeyError)
    assert raised.value.args == ("found",)
