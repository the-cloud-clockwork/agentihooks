"""A tick step that raises shuts only the steps that need it, directly or through another; the pass runs the rest."""

from collections.abc import Callable
from functools import partial
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import cli
from scripts.tick import registry
from scripts.tick import sites as tick_sites
from scripts.tick import steps as tick_steps
from scripts.tick.step import Context, Result
from scripts.tick.steps import host, masters, observe, tasks
from tests.swarm.test_tick_groups import _Marker
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


def _raising(tick: object, get: object) -> list[str]:
    """Raise as a held tick call."""
    raise RuntimeError(SWEEP_DOWN)


def _observed(slug: object) -> list[str]:
    """Raise as an observation step function given the swarm."""
    raise RuntimeError(SWEEP_DOWN)


def _declared(box: dict[str, Any], step: str) -> Callable[[str], Any]:
    """Return a pass reader handing step ``step`` the declared view of ``box`` and nothing else."""
    declared = tick_steps.DeclaredBox(box, step, frozenset())
    return lambda key: declared if key == "box" else None


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


@pytest.mark.feature("swarm-tick-step-failure-isolated")
def test_a_raised_step_shuts_only_the_steps_that_need_it_directly_or_through_another() -> None:
    box = tick_steps.PassBox()
    tick_steps.hold(tick_steps.DeclaredBox(box, "exits sweep", frozenset()), RuntimeError(SWEEP_DOWN))
    sites = tick_sites.TickSites({}, box, {}, lambda: 0)
    assert sites.shut(_Marker("reader", "observe", needs=frozenset({"exits sweep"})))
    assert sites.shut(_Marker("reader of reader", "agents", needs=frozenset({"reader"})))
    assert not sites.shut(_Marker("reap", "agents", needs=frozenset({"ledger state", "automated cleanup"})))
    assert not sites.shut(_Marker("verify", "observe"))
    assert list(box) == ["raised", "fallen"]
    assert box["fallen"] == {"exits sweep", "reader", "reader of reader"}


@pytest.mark.feature("swarm-tick-step-failure-isolated")
def test_a_pass_without_a_raise_shuts_no_step_for_its_needs() -> None:
    box = tick_steps.PassBox()
    sites = tick_sites.TickSites({}, box, {}, lambda: 0)
    assert not sites.shut(_Marker("reader", "observe", needs=frozenset({"exits sweep"})))
    assert dict(box) == {}


@pytest.mark.feature("swarm-tick-step-failure-isolated")
def test_the_first_held_exception_stays_the_one_the_pass_ends_on() -> None:
    box = tick_steps.PassBox()
    first, later = RuntimeError("first"), ValueError("later")
    tick_steps.hold(tick_steps.DeclaredBox(box, "exits sweep", frozenset()), first)
    tick_steps.hold(tick_steps.DeclaredBox(box, "verify", frozenset()), later)
    plain: dict[str, Any] = {}
    tick_steps.hold(plain, later)
    assert box["raised"] is first
    assert box["fallen"] == {"exits sweep", "verify"}
    assert plain == {"raised": later}
    with pytest.raises(RuntimeError, match="^first$"):
        observe.raise_held(box)


@pytest.mark.feature("swarm-tick-step-failure-isolated")
@pytest.mark.parametrize(
    "wrapped",
    [partial(observe._run, _observed), masters._held(_raising), tasks.held(_raising), host.held(_raising)],
    ids=["observe", "masters", "tasks", "host"],
)
def test_each_step_wrapper_holds_what_its_step_raises_and_names_the_step(
    wrapped: Callable[[Any, Callable[[str], Any]], Any],
) -> None:
    box = tick_steps.PassBox()
    assert wrapped(None, _declared(box, "exits sweep")) == []
    assert isinstance(box["raised"], RuntimeError)
    assert box["fallen"] == {"exits sweep"}


@pytest.mark.feature("swarm-tick-step-failure-isolated")
def test_the_wrapper_asks_the_tick_sites_first_so_a_stopped_step_needing_a_raised_step_falls_too() -> None:
    box = tick_steps.PassBox({"stop": ["Ledger health probe failed"]})
    tick_steps.hold(tick_steps.DeclaredBox(box, "ledger health", frozenset()), RuntimeError(SWEEP_DOWN))
    wrapper = SimpleNamespace(box=box, ended=None, ticks=tick_sites.TickSites({}, box, {}, lambda: 0))
    stopped = _Marker("command runner", "wrapper commands", needs=frozenset({"ledger health"}))
    opened = _Marker("outbox migration", "wrapper relay", needs=frozenset({"command runner"}))
    assert cli._Wrapper.shut(wrapper, stopped)  # pyright: ignore[reportPrivateUsage, reportArgumentType]
    assert cli._Wrapper.shut(wrapper, opened)  # pyright: ignore[reportPrivateUsage, reportArgumentType]
    assert box["fallen"] == {"ledger health", "command runner", "outbox migration"}
