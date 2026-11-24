"""The tick pass holds its ledger view once, before the step pool starts the critical lane clock."""

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from typing import Any

import pytest

from scripts.swarm import cli
from scripts.swarm.store import RedisStore
from scripts.tick import pool
from scripts.tick import view as ledger_view
from tests.swarm.test_cli import env, run
from tests.swarm.test_delivery import FakeHerdr

__all__ = ["env"]

STATES = ("running", "stopped")
HELD_VIEW = ledger_view.LedgerView(7, {"tasks": []})
VIEW_HELD = "view held"
VIEW_HELD_INSIDE_THE_LANE = "view held inside the lane"
LANE_CLOCK_STARTED = "lane clock started"


def _ordered_pass(env: tuple[RedisStore, Any, Any], monkeypatch: pytest.MonkeyPatch, state: str) -> list[str]:
    """Run one pass of swarm ``sw`` in ``state`` and return its view holds and step pool starts in the order they
    happened, each hold named by whether the step pool had already started its lane clock."""
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    store.update("sw", state=state)
    order: list[str] = []
    real_hold, real_running = cli._hold_view, pool.running

    def hold(beating: ExitStack, slug: str, held_ledger: Any) -> None:
        order.append(VIEW_HELD if pool.active() is None else VIEW_HELD_INSIDE_THE_LANE)
        real_hold(beating, slug, held_ledger)

    @contextmanager
    def running(*args: Any, **kwargs: Any) -> Generator[pool.Pass, None, None]:
        order.append(LANE_CLOCK_STARTED)
        with real_running(*args, **kwargs) as found:
            yield found

    monkeypatch.setattr(cli, "_hold_view", hold)
    monkeypatch.setattr(pool, "running", running)
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    return order


@pytest.mark.feature("swarm-tick-view-held-before-lane-clock")
@pytest.mark.parametrize("state", STATES, ids=STATES)
def test_a_pass_holds_its_ledger_view_exactly_once(
    env: tuple[RedisStore, Any, Any], monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """A running and a stopped pass each hold their ledger view once and start the step pool once."""
    order = _ordered_pass(env, monkeypatch, state)
    assert len([seen for seen in order if seen != LANE_CLOCK_STARTED]) == 1
    assert order.count(LANE_CLOCK_STARTED) == 1


@pytest.mark.feature("swarm-tick-view-held-before-lane-clock")
def test_the_sites_of_a_running_pass_read_the_view_the_pass_holds(
    env: tuple[RedisStore, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sites of a running pass read the view its hold set, and the pass leaves no view held when it ends."""
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    store.update("sw", state="running")
    read: list[ledger_view.LedgerView | None] = []
    real_sites = cli.run_sites

    def hold(beating: ExitStack, slug: str, held_ledger: Any) -> None:
        beating.enter_context(ledger_view.holding())
        ledger_view.CURRENT.set(HELD_VIEW)
        ledger_view.HELD.set(ledger_view.Held(HELD_VIEW, lambda: HELD_VIEW))

    def sites(*args: Any, **kwargs: Any) -> Any:
        read.append(ledger_view.current())
        return real_sites(*args, **kwargs)

    monkeypatch.setattr(cli, "_hold_view", hold)
    monkeypatch.setattr(cli, "run_sites", sites)
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert read == [HELD_VIEW]
    assert ledger_view.HELD.get() is None


@pytest.mark.feature("swarm-tick-view-held-before-lane-clock")
@pytest.mark.parametrize("state", STATES, ids=STATES)
def test_the_pass_holds_its_ledger_view_before_the_step_pool_starts_the_lane_clock(
    env: tuple[RedisStore, Any, Any], monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """The view hold, with its delta read and whole view store, runs before the step pool starts the sixty second
    critical lane clock, so a slow store never spends the lane of the steps after it."""
    assert _ordered_pass(env, monkeypatch, state) == [VIEW_HELD, LANE_CLOCK_STARTED]
