"""Two master starters in one pass launch exactly one front door master, because each takes the swarm's master
launch lock before it reads the master seats and keeps it through the launch."""

import json
from collections.abc import Callable
from typing import Any

import pytest

from scripts.swarm import master_fallback, master_launch
from scripts.swarm.store import SwarmError
from scripts.tick.steps import masters
from tests.swarm.test_cli import env  # noqa: F401
from tests.swarm.test_master_launch import ResumingRuntime, up  # noqa: F401

pytestmark = pytest.mark.unit

NOW = 1_791_380_000_000
WATCHED = {master_fallback.SWARMS: "sw", master_fallback.MINUTES: "0"}


@pytest.fixture
def swarm(up: tuple[Any, Any, ResumingRuntime]) -> tuple[Any, ResumingRuntime]:  # noqa: F811
    """The created swarm sw with no master, and its runtime."""
    store, _, rt = up
    return store, rt


def master_up(store: Any, rt: ResumingRuntime, choice: str, refusals: list[str]) -> Callable[[str], int]:
    """The launch a fallback starter's detached process runs, master up with ``choice``, run in this process and
    answering its exit code; each refusal it prints is kept in ``refusals``."""

    def launch(slug: str) -> int:
        """Run master up for ``slug`` and answer 0 when it launched, 1 when it refused."""
        try:
            master_launch.up(store, slug, rt, NOW, choice, input, print)
        except SwarmError as refused:
            refusals.append(str(refused))
            return 1
        return 0

    return launch


@pytest.mark.feature("master-launch-lock")
@pytest.mark.parametrize("choice", [master_launch.NEW, master_launch.REPLACE])
def test_a_fallback_starter_racing_the_tick_master_step_launches_exactly_one_master(
    swarm: tuple[Any, ResumingRuntime], monkeypatch: pytest.MonkeyPatch, choice: str
) -> None:
    store, rt = swarm
    capacity, passes, refusals = rt.has_capacity, [], []
    launch = master_up(store, rt, choice, refusals)

    def racing(config: Any) -> bool:
        """Run the master fallback pass once, after the tick read the master seats and before it launches."""
        if not passes:
            passes.append([])
            passes.append(master_fallback.check(store, WATCHED, rt.live_names, NOW, {}, launch))
        return capacity(config)

    monkeypatch.setattr(rt, "has_capacity", racing)
    said = masters._master("sw", store.config("sw"), store, rt, NOW)
    assert len(rt.masters) == 1
    assert said[0] == f"spawned master {rt.masters[0][0]}"
    assert passes[1] == [json.dumps({"swarm": "sw", "master_up": 1, "at": NOW})]
    assert refusals == ["the master launch lock of swarm sw is held by tick master, so no second master starts"]


@pytest.mark.feature("master-launch-lock")
def test_the_tick_master_step_waits_while_master_up_launches(
    swarm: tuple[Any, ResumingRuntime], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, rt = swarm
    spawn, ticked = rt.spawn, []

    def racing(config: Any, lane: str, name: str, task: dict[str, Any]) -> Any:
        """Run the tick master step once while master up launches its master."""
        if not ticked:
            ticked.append(masters._master("sw", store.config("sw"), store, rt, NOW))
        return spawn(config, lane, name, task)

    monkeypatch.setattr(rt, "spawn", racing)
    master_launch.up(store, "sw", rt, NOW, master_launch.NEW, input, print)
    assert ticked == [["the master launch lock of swarm sw is held by master up, so no second master starts"]]
    assert len(rt.masters) == 1
