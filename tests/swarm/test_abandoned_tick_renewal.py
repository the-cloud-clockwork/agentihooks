"""A step worker the pass abandoned at its budget never renews the tick lock once its pass released it."""

from threading import Event
from typing import Any

import pytest

from scripts.gates import progress
from scripts.swarm import cli, commands, controller, lease, tick_activity, timing
from scripts.tick import engine
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Result
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr
from tests.tick.test_engine import FakeStep, View

pytestmark = pytest.mark.unit


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    now = [1000]
    monkeypatch.setattr(lease, "now_ms", lambda saved: now[0])
    return now


@pytest.mark.feature("swarm-tick-abandoned-renewal")
@pytest.mark.parametrize("name", ["metrics record", "status publish"])
def test_an_abandoned_step_worker_never_renews_the_lock_its_pass_released(
    env: Any,  # noqa: F811
    clock: list[int],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    waiting, resume, ended = Event(), Event(), Event()
    errors: list[BaseException] = []
    calls: list[str] = []
    records: list[engine.StepRecord] = []

    def late(ctx: Context) -> Result:
        waiting.set()
        assert resume.wait(5)
        try:
            timing.call(calls.append, "late")
        except BaseException as exc:
            errors.append(exc)
        finally:
            ended.set()
        return Result()

    def abandon(*args: Any, **kwargs: Any) -> list[str]:
        records.append(engine._run_once(FakeStep(name, late, budget_s=0.1), Context(View(), Deadline.after(5))))
        return []

    monkeypatch.setattr(progress, "checks_pass", abandon)
    try:
        cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
        assert waiting.is_set()
        assert [record.abandoned for record in records] == [True]
        key = store.key("sw", "tick-lock")
        assert not store.redis.exists(key)
        held = lease.acquire(store, "sw", commands.hive_id())
        assert held is not None
        successor = controller.take_tick_lock(store, "sw", held, 1000, "successor")
        assert successor is not None
        lock = store.redis.get(key)
        activity = tick_activity.read(store, "sw")
        resume.set()
        assert ended.wait(5)
        assert errors == []
        assert calls == ["late"]
        assert store.redis.get(key) == lock
        assert store.redis.pttl(key) <= 1000
        assert tick_activity.read(store, "sw") == activity
    finally:
        resume.set()
