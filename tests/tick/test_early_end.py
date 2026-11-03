"""A pass that ends early on a value no step stored still stamps its finish and names the value and the step left."""

from threading import Event
from typing import Any

import pytest

from scripts.swarm import cli, tick_heartbeat
from scripts.tick import engine
from scripts.tick.step import Context
from scripts.tick.steps import Left, PassBox, host
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr

pytestmark = pytest.mark.unit


@pytest.mark.feature("swarm-tick-early-end-stamp")
def test_a_held_tick_call_still_stamps_the_pass_and_names_the_missing_value(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    release = Event()
    real = engine.run_group

    def slow_tick(*args: Any) -> list[str]:
        release.wait(30.0)
        return []

    def group(
        name: str, ctx: Context, steps: Any = None, apply: Any = None, finished: Any = None
    ) -> tuple[engine.StepRecord, ...]:
        if name == "wrapper relay":
            return (engine.StepRecord("page relay", 0.0, 0.01, abandoned=True),)
        return real(name, ctx, steps, apply, finished)

    step = next(step for step in host.STEPS if step.name == "tick call")
    monkeypatch.setattr(step, "budget_s", 0.01)
    monkeypatch.setattr(cli, "tick", slow_tick)
    monkeypatch.setattr(engine, "run_group", group)
    try:
        actions = cli.run_tick(swarm, "sw", ledger, runtime, FakeHerdr({}))
        assert swarm.redis.get(swarm.key("sw", "last-tick")) is not None
        assert tick_heartbeat.read(swarm, "sw") is not None
        assert len(actions) == 1
        assert "agents" in actions[0]
        assert "tick call" in actions[0]
        assert "page relay" in actions[0]
    finally:
        release.set()
        worker = engine._RUNNING.get(("sw", "tick call"))
        if worker is not None:
            worker.join(30.0)


@pytest.mark.feature("swarm-tick-early-end-stamp")
def test_a_pass_box_shares_the_names_of_the_steps_its_pass_left() -> None:
    box = PassBox()
    judging = PassBox(left=box.left)
    judging.left.append("intent judge")
    assert box.left == ["intent judge"]
    with pytest.raises(Left):
        judging["judged"]
