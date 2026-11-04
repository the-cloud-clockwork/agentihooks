"""The pass finally runs the heartbeat, last tick and status publish on a closing lane the critical lane never drops."""

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from time import sleep
from typing import Any

import pytest

from scripts.swarm import cli, reboot, tick_heartbeat
from scripts.tick import engine, pool
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result
from scripts.tick.steps import host, report
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr

pytestmark = pytest.mark.unit


class _View:
    def read(self, domain: Domain, key: str) -> object | None:
        return None


@dataclass
class _Step:
    name: str
    action: Callable[[Context], Result]
    lane: Lane
    needs: frozenset[str] = frozenset()
    group: str = "closing"
    budget_s: float = 2.0
    interval_s: float = 0.0
    reads: frozenset[Domain] = frozenset()
    writes: frozenset[Domain] = frozenset()

    def run(self, ctx: Context) -> Result:
        return self.action(ctx)


def test_the_closing_lane_starts_once_when_the_pass_closes_and_outlives_the_critical_lane() -> None:
    with pool.running(critical_s=0.01, closing_s=5.0) as current:
        sleep(0.05)
        assert current.critical.expired
        closing = current.close()
        assert current.close() is closing
        assert current.lane(pool.CLOSING_LANE) is closing
        assert closing is not current.critical
        assert closing is not current.slow
        assert current.closing_s == 5.0
        assert 0.0 < closing.remaining <= 5.0


def test_the_closing_lane_is_unstarted_until_the_pass_closes() -> None:
    current = pool.Pass(pool.PARALLEL, 1, Deadline.after(1.0), Deadline.after(1.0), pool.BoundedSemaphore(1))
    assert current.closer == []
    assert current.closing_s == pool.CLOSING_S == 30.0
    current.close()
    assert len(current.closer) == 1


def test_closing_steps_run_after_the_critical_lane_expired_while_critical_steps_drop() -> None:
    steps = (
        _Step("closing step", lambda ctx: Result(), Lane(pool.CLOSING_LANE)),
        _Step("critical step", lambda ctx: Result(), Lane("tick")),
    )
    with pool.running(critical_s=0.01) as current:
        sleep(0.05)
        current.close()
        records = engine.run_group("closing", Context(_View(), Deadline.after(5.0)), steps)
    assert [(record.name, record.outcome) for record in records] == [
        ("closing step", engine.OK),
        ("critical step", engine.LATE),
    ]
    assert records[1].abandoned


def test_the_three_closing_steps_sit_in_the_closing_group_and_lane() -> None:
    named = {step.name: step for step in report.STEPS if step.group == "closing"}
    assert sorted(named) == ["heartbeat", "last tick", "status publish"]
    assert {step.lane for step in named.values()} == {pool.CLOSING_LANE}


def test_a_planted_300_second_step_under_a_1_second_critical_lane_still_stamps_and_publishes(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    swarm.update("sw", tick_mode=pool.PARALLEL)
    release = Event()
    real_group, real_running = engine.run_group, pool.running
    closing: list[engine.StepRecord] = []

    def planted(*args: Any) -> list[str]:
        release.wait(300.0)
        return []

    def group(
        name: str, ctx: Context, steps: Any = None, apply: Any = None, finished: Any = None
    ) -> tuple[engine.StepRecord, ...]:
        records = real_group(name, ctx, steps, apply, finished)
        if name == "closing":
            closing.extend(records)
        return records

    step = next(step for step in host.STEPS if step.name == "reboot pass")
    monkeypatch.setattr(step, "budget_s", 300.0)
    monkeypatch.setattr(reboot, "boot_pass", planted)
    monkeypatch.setattr(engine, "run_group", group)
    monkeypatch.setattr(pool, "running", lambda **kw: real_running(**kw, critical_s=1.0))
    try:
        actions = cli.run_tick(swarm, "sw", ledger, runtime, FakeHerdr({}))
        assert actions[0].startswith("pass ended early: no step stored ")
        assert swarm.redis.get(swarm.key("sw", "last-tick")) is not None
        assert tick_heartbeat.read(swarm, "sw") is not None
        outcomes = {record.name: record.outcome for record in closing}
        assert outcomes == {"heartbeat": engine.OK, "last tick": engine.OK, "status publish": engine.OK}
    finally:
        release.set()
        worker = engine._RUNNING.get(("sw", "reboot pass"))
        if worker is not None:
            worker.join(30.0)
