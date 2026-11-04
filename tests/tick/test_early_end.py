"""A pass that ends early on a value no step stored still stamps its finish and names the value and the step left."""

from threading import Event
from typing import Any

import pytest

from scripts.swarm import cli, reboot, tick_heartbeat
from scripts.swarm.store import RedisStore
from scripts.tick import engine
from scripts.tick.step import Context
from scripts.tick.steps import Left, PassBox, host, report
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr

pytestmark = pytest.mark.unit


@pytest.mark.feature("swarm-tick-early-end-stamp")
def test_a_held_reboot_pass_still_stamps_the_pass_and_names_the_missing_value(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    release = Event()
    real = engine.run_group

    def slow_boot(*args: Any) -> list[str]:
        release.wait(30.0)
        return []

    def group(
        name: str, ctx: Context, steps: Any = None, apply: Any = None, finished: Any = None
    ) -> tuple[engine.StepRecord, ...]:
        if name == "wrapper relay":
            return (engine.StepRecord("page relay", 0.0, 0.01, abandoned=True),)
        return real(name, ctx, steps, apply, finished)

    step = next(step for step in host.STEPS if step.name == "reboot pass")
    monkeypatch.setattr(step, "budget_s", 0.01)
    monkeypatch.setattr(reboot, "boot_pass", slow_boot)
    monkeypatch.setattr(engine, "run_group", group)
    try:
        actions = cli.run_tick(swarm, "sw", ledger, runtime, FakeHerdr({}))
        assert swarm.redis.get(swarm.key("sw", "last-tick")) is not None
        assert tick_heartbeat.read(swarm, "sw") is not None
        assert len(actions) == 1
        assert actions[0].startswith("pass ended early: no step stored agents, the pass left ")
        assert "reboot pass" in actions[0]
        assert "page relay" in actions[0]
    finally:
        release.set()
        worker = engine._RUNNING.get(("sw", "reboot pass"))
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


@pytest.mark.feature("swarm-tick-early-end-stamp")
@pytest.mark.parametrize(
    ("left", "named"),
    [(["reboot pass", "page relay", "reboot pass"], "reboot pass, page relay"), ([], "no recorded step")],
)
def test_an_early_end_stamps_the_pass_at_its_time_and_names_each_left_step_once(left: list[str], named: str) -> None:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found = report.early_end(store, "sw", 12345, {"_meta": {"rev": 7}}, Left("agents"), left)
    assert found == [f"pass ended early: no step stored agents, the pass left {named}"]
    assert store.redis.get(store.key("sw", "last-tick")) == "12345"
    beat = tick_heartbeat.read(store, "sw")
    assert beat is not None
    assert beat.at == 12345
    assert beat.rev == 7
