"""A step worker the pass abandoned at its budget never renews the tick lock once its pass released it."""

from collections.abc import Callable
from threading import Event
from typing import Any

import pytest

from scripts.gates import progress
from scripts.swarm import cli, command_runner, commands, controller, lease, metrics, tick_activity, timing
from scripts.swarm.store import RedisStore
from scripts.tick import engine
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Result
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr
from tests.tick.test_engine import FakeStep, View

pytestmark = pytest.mark.unit


class Box:
    def append(self, table: Any, rows: Any) -> None:
        return None

    def flush(self, now_ms: int) -> None:
        return None

    def close(self) -> None:
        return None


def quiet(*args: Any, **kwargs: Any) -> Any:
    return None


def recorder(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr(metrics.metrics_outbox, "settings", quiet)
    monkeypatch.setattr(metrics.metrics_outbox, "Outbox", lambda path, sink: Box())
    monkeypatch.setattr(metrics, "_pass", quiet)
    return timing.call(metrics.record_pass, "sw", 1, 0)


def publisher(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command_runner.ledger_workspace, "mirror", quiet)
    monkeypatch.setattr(command_runner.ledger_workspace, "published", quiet)
    monkeypatch.setattr(command_runner, "status_report", lambda *args: {})
    monkeypatch.setattr(command_runner.commands, "publish", quiet)
    return timing.call(command_runner.publish, store, "sw", {})


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    now = [1000]
    monkeypatch.setattr(lease, "now_ms", lambda saved: now[0])
    return now


@pytest.mark.feature("swarm-tick-abandoned-renewal")
@pytest.mark.parametrize(("name", "closing"), [("metrics record", recorder), ("status publish", publisher)])
def test_an_abandoned_step_worker_never_renews_the_lock_its_pass_released(
    env: Any,  # noqa: F811
    clock: list[int],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    closing: Callable[[RedisStore, pytest.MonkeyPatch], object],
) -> None:
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    waiting, resume, ended = Event(), Event(), Event()
    errors: list[BaseException] = []
    records: list[engine.StepRecord] = []

    def late(ctx: Context) -> Result:
        waiting.set()
        assert resume.wait(5)
        try:
            closing(store, monkeypatch)
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
        assert store.redis.get(key) == lock
        assert store.redis.pttl(key) <= 1000
        assert tick_activity.read(store, "sw") == activity
    finally:
        resume.set()
