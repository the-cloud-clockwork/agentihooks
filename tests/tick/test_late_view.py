"""A late event view refresh retains the earlier snapshot and completes the wrapper pass."""

from collections.abc import Callable
from threading import Event
from typing import Any

import pytest

from scripts.swarm import cli, ledger_events
from scripts.tick import engine
from scripts.tick.step import Context
from scripts.tick.steps import PASS, budgets, host
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr

pytestmark = pytest.mark.unit

SLOWEST_OBSERVED_VIEW_S = 2.88


@pytest.mark.feature("swarm-tick-late-view")
def test_the_view_budget_covers_the_measured_ledger_read() -> None:
    assert budgets.MEASURED["tick view"] == budgets.MEASURED["ledger state"]
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
    seen: list[tuple[str, Context, tuple[engine.StepRecord, ...]]] = []
    real = engine.run_group

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

    def group(
        name: str, ctx: Context, steps: Any = None, apply: Any = None, finished: Any = None
    ) -> tuple[engine.StepRecord, ...]:
        if name == "wrapper view" and outcome == engine.HELD:
            records = (engine.StepRecord("tick view", 0.0, 0.01, held=True),)
        else:
            records = real(name, ctx, steps, apply, finished)
        seen.append((name, ctx, records))
        return records

    step = next(step for step in host.STEPS if step.name == "tick view")
    monkeypatch.setattr(step, "budget_s", 30.0 if outcome == engine.OK else 0.01)
    monkeypatch.setattr(ledger_events, "tick_view", tick_view)
    monkeypatch.setattr(engine, "run_group", group)
    try:
        cli.run_tick(swarm, "sw", ledger, runtime, FakeHerdr({}))
        sites = {name: (ctx, records) for name, ctx, records in seen}
        assert sites["wrapper view"][1][0].outcome == outcome
        expected = refreshed if outcome == engine.OK else earlier
        for name in ("wrapper events", "wrapper metrics", "closing"):
            ctx, records = sites[name]
            assert ctx.view.read(PASS, "view") is expected
            assert records
            assert all(record.outcome == engine.OK for record in records)
        assert any(record.name == "metrics record" for record in sites["wrapper metrics"][1])
        assert any(record.name == "heartbeat" for record in sites["closing"][1])
        assert swarm.redis.get(swarm.key("sw", "last-tick")) is not None
    finally:
        release.set()
        worker = engine._RUNNING.get(("sw", "tick view"))
        if worker is not None:
            worker.join(30.0)
