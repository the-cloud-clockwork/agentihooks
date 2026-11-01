"""The tick and the tick wrapper run the engine's registered steps at each group site, in legacy order."""

from dataclasses import dataclass, field
from time import monotonic
from typing import Any

import pytest

from scripts.swarm import cli
from scripts.swarm import tick as tick_module
from scripts.swarm.store import RedisStore
from scripts.swarm.tick import tick
from scripts.tick import engine, registry
from scripts.tick.step import Context, Domain, Lane, Result
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_tick import FakeRuntime, store, tasks  # noqa: F401

pytestmark = pytest.mark.unit

TICK_GROUPS = ["start", "observe", "agents", "masters", "tasks", "place", "close"]


def _spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Context]]:
    seen: list[tuple[str, Context]] = []
    real = engine.run_group

    def run_group(name: str, ctx: Context) -> tuple[engine.StepRecord, ...]:
        seen.append((name, ctx))
        return real(name, ctx)

    monkeypatch.setattr(tick_module.engine, "run_group", run_group)
    return seen


@dataclass(frozen=True)
class _Marker:
    group: str
    ran: list[str] = field(default_factory=list)
    name: str = "marker"
    lane: Lane = Lane("eng")
    order: int = 0
    budget_s: float = 5.0
    interval_s: float = 0.0
    needs: frozenset[str] = frozenset()
    writes: frozenset[Domain] = frozenset()

    def run(self, ctx: Context) -> Result:
        self.ran.append(self.group)
        return Result()


def test_a_tick_pass_runs_the_seven_groups_in_order(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    seen = _spy(monkeypatch)
    tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), now_ms=1_000)
    assert [name for name, _ in seen] == TICK_GROUPS


def test_each_group_reads_an_empty_view_under_the_group_budget(
    store: RedisStore,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _spy(monkeypatch)
    tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), now_ms=1_000)
    for _, ctx in seen:
        assert ctx.view.read(Domain("tasks"), "t1") is None
        assert ctx.deadline.expires_at - monotonic() == pytest.approx(tick_module.GROUP_BUDGET_S, abs=5.0)
    assert tick_module.GROUP_BUDGET_S == 600.0


def test_a_stopped_swarm_runs_only_the_start_group(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    store.update("sw", state="stopped")
    seen = _spy(monkeypatch)
    tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), now_ms=1_000)
    assert [name for name, _ in seen] == ["start"]


def test_a_registered_step_runs_in_its_tick_group(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(registry, "registry", registry.Registry())
    ran: list[str] = []
    registry.register(_Marker("agents", ran))
    tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), now_ms=1_000)
    assert ran == ["agents"]


def test_the_tick_wrapper_runs_its_groups_before_and_after_the_tick(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    seen = _spy(monkeypatch)
    cli.run_tick(swarm, "sw", ledger, rt, FakeHerdr({}))
    assert [name for name, _ in seen] == ["before", *TICK_GROUPS, "after"]
