"""A tick step that raises shuts only the steps that need it, directly or through another; the pass runs the rest."""

from collections.abc import Callable
from importlib import import_module
from pathlib import Path

import pytest

from scripts.tick import registry
from scripts.tick.step import Context, Result
from tests.swarm.tick_scenarios.test_characterisation import replay

tick_module = import_module("scripts.swarm.tick")
SWEEP_DOWN = "no agent name for type 'engineer', code '323133', number 10000"


def _recorded(run: Callable[[Context], Result], name: str, ran: set[str]) -> Callable[[Context], Result]:
    """Return ``run`` naming step ``name`` in ``ran`` each time the engine runs it."""

    def recorded(ctx: Context) -> Result:
        ran.add(name)
        return run(ctx)

    return recorded


def _ran(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Return the names of the registered steps the engine runs from now on."""
    ran: set[str] = set()
    for step in registry.steps():
        monkeypatch.setattr(step, "run", _recorded(step.run, step.name, ran))
    return ran


def _sweep_down(*args: object) -> list[str]:
    """Raise as the inbox exits sweep raised in every pass from 07:15 to 09:27 UTC on 16 November."""
    raise RuntimeError(SWEEP_DOWN)


@pytest.mark.feature("swarm-tick-step-failure-isolated")
def test_a_raising_exits_sweep_still_runs_reap_and_the_master_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ran = _ran(monkeypatch)
    monkeypatch.setattr(tick_module.exits, "sweep", _sweep_down)
    with pytest.raises(RuntimeError, match=SWEEP_DOWN):
        replay("running", "tick", monkeypatch, tmp_path)
    assert {"exits sweep", "reap", "tick master"} <= ran


@pytest.mark.feature("swarm-tick-step-failure-isolated")
def test_a_fenced_pass_with_a_raising_exits_sweep_runs_reap_and_the_master_step_and_ends_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ran = _ran(monkeypatch)
    monkeypatch.setattr(tick_module.exits, "sweep", _sweep_down)
    record = replay("running", "run_tick", monkeypatch, tmp_path)
    assert record["actions"] == [[f"pass failed: a step raised RuntimeError: {SWEEP_DOWN}"]]
    assert {"exits sweep", "reap", "tick master"} <= ran
