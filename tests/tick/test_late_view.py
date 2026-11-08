"""A late event view refresh retains the earlier snapshot and completes the wrapper pass."""

from collections.abc import Callable
from threading import Event, Thread
from typing import Any

import pytest

from scripts.swarm import cli, ledger_events, stall_watch
from scripts.tick import engine
from scripts.tick.step import Context
from scripts.tick.steps import PASS, budgets, host
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr

pytestmark = pytest.mark.unit

SLOWEST_OBSERVED_VIEW_S = 2.88


@pytest.mark.feature("swarm-tick-late-view")
def test_the_view_budget_covers_the_measured_ledger_read() -> None:
    assert budgets.FLOORS["tick view"] == budgets.FLOORS["ledger state"]
    step = next(step for step in host.STEPS if step.name == "tick view")
    assert step.budget_s == budgets.timing_of("ledger state")[0]
    assert step.budget_s >= SLOWEST_OBSERVED_VIEW_S


@pytest.mark.feature("swarm-tick-late-view")
@pytest.mark.parametrize("outcome", [engine.LATE, engine.HELD, engine.OK])
def test_a_late_view_keeps_events_metrics_and_heartbeat_running(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    swarm, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    release = Event()
    graphs: list[tuple[tuple[Any, ...], Context, tuple[engine.StepRecord, ...]]] = []
    groups: list[tuple[str, Context, tuple[engine.StepRecord, ...]]] = []
    real_graph, real_group = engine.run_graph, engine.run_group

    def earlier(url: str) -> None:
        return None

    def refreshed(url: str) -> None:
        return None

    def tick_view(
        inbox: object, store: object, slug: str, doc: dict[str, Any], previous: Callable[..., Any] | None = None
    ) -> Callable[..., Any]:
        assert store is swarm
        assert slug == "sw"
        assert isinstance(doc["tasks"], list)
        if previous is None:
            return earlier
        assert previous is earlier
        if outcome != engine.OK:
            assert release.wait(30.0)
        return refreshed

    def graph(steps: Any, ctx: Context, *args: Any) -> tuple[engine.StepRecord, ...]:
        found = tuple(steps)
        records = real_graph(found, ctx, *args)
        graphs.append((found, ctx, records))
        return records

    def group(
        name: str, ctx: Context, steps: Any = None, apply: Any = None, finished: Any = None
    ) -> tuple[engine.StepRecord, ...]:
        records = real_group(name, ctx, steps, apply, finished)
        groups.append((name, ctx, records))
        return records

    step = next(step for step in host.STEPS if step.name == "tick view")
    monkeypatch.setattr(step, "floor_s", 30.0 if outcome == engine.OK else 0.01)
    monkeypatch.setattr(ledger_events, "tick_view", tick_view)
    monkeypatch.setattr(engine, "run_graph", graph)
    monkeypatch.setattr(engine, "run_group", group)
    earlier_run = Thread(target=release.wait, args=(30.0,), daemon=True)
    if outcome == engine.HELD:
        earlier_run.start()
        monkeypatch.setitem(engine._RUNNING, ("sw", "tick view"), earlier_run)
    try:
        cli.run_tick(swarm, "sw", ledger, runtime, FakeHerdr({}))
        [(steps, ctx, records)] = graphs
        sites = {step.name: step.group for step in steps}
        named = {record.name: record for record in records}
        assert named["tick view"].outcome == outcome
        expected = refreshed if outcome == engine.OK else earlier
        assert ctx.view.read(PASS, "view") is expected
        for site in ("wrapper events", "wrapper metrics"):
            ran = [record for record in records if sites[record.name] == site]
            assert ran
            assert all(record.outcome == engine.OK for record in ran)
        assert "metrics record" in named
        closing = {name: (ctx, records) for name, ctx, records in groups}["closing"]
        assert closing[0].view.read(PASS, "view") is expected
        assert any(record.name == "heartbeat" for record in closing[1])
        assert swarm.redis.get(swarm.key("sw", "last-tick")) is not None
    finally:
        release.set()
        worker = engine._RUNNING.get(("sw", "tick view"))
        if worker is not None:
            worker.join(30.0)
        if earlier_run.is_alive():
            earlier_run.join(30.0)


@pytest.mark.feature("swarm-tick-late-view")
def test_an_abandoned_view_skips_the_stall_watch_and_the_pass_ends(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    swarm, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    release: Event = Event()
    watched: list[Any] = []

    def tick_view(*args: Any) -> Callable[..., Any]:
        assert release.wait(30.0)
        return lambda url: None

    def stall_pass(watch: Any, doc: dict[str, Any]) -> list[str]:
        watched.append(watch.view("https://github.com/o/r/pull/1"))
        return []

    step = next(step for step in host.STEPS if step.name == "tick view")
    monkeypatch.setattr(step, "floor_s", 0.01)
    monkeypatch.setattr(cli, "_intent_pass", lambda *args: (None, []))
    monkeypatch.setattr(ledger_events, "tick_view", tick_view)
    monkeypatch.setattr(stall_watch, "stall_pass", stall_pass)
    try:
        cli.run_tick(swarm, "sw", ledger, runtime, FakeHerdr({}))
    finally:
        release.set()
        worker = engine._RUNNING.get(("sw", "tick view"))
        if worker is not None:
            worker.join(30.0)
    assert watched == []
    assert "skipped stall watch: no step stored the tick view this pass" in capsys.readouterr().err


@pytest.mark.feature("swarm-tick-late-view")
def test_an_error_a_step_held_ends_the_pass_failed_and_the_next_pass_runs(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")

    def stall_pass(watch: Any, doc: dict[str, Any]) -> list[str]:
        raise TypeError("'NoneType' object is not callable")

    monkeypatch.setattr(ledger_events, "tick_view", lambda *args: lambda url: None)
    monkeypatch.setattr(stall_watch, "stall_pass", stall_pass)
    first = cli.run_tick(swarm, "sw", ledger, runtime, FakeHerdr({}))
    assert first == ["pass failed: a step raised TypeError: 'NoneType' object is not callable"]
    assert swarm.redis.get(swarm.key("sw", "tick-lock")) is None
    second = cli.run_tick(swarm, "sw", ledger, runtime, FakeHerdr({}))
    assert second == first
