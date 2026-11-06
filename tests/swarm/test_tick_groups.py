"""The tick and the tick wrapper run the engine's registered steps as one flat pass graph, site by site."""

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from threading import Event
from time import monotonic
from typing import Any

import pytest

from scripts.inbox import wake
from scripts.swarm import cli, command_runner, controller, reboot, snapshot_job
from scripts.swarm import tick as tick_module
from scripts.swarm.store import RedisStore
from scripts.swarm.tick import tick
from scripts.tick import engine, registry, sites
from scripts.tick.adapters.inbox import StoreInbox
from scripts.tick.ports import Tools
from scripts.tick.step import Context, Domain, Lane, Result, Step, Write
from scripts.tick.steps import PASS, actions_of, observe, report
from scripts.tick.steps import tasks as task_steps
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_tick import FakeRuntime, store, tasks  # noqa: F401

pytestmark = pytest.mark.unit

TICK_GROUPS = [
    "start",
    "pass setup",
    "observe",
    "agents",
    "masters",
    "drained resume",
    "tasks",
    "reopen passes",
    "task passes",
    "place",
    "placement notices",
    "placement acks",
    "master seats",
    "placement",
    "dispatch seat",
    "master watch",
    "close",
]
WRAPPER_GROUPS = [
    "wrapper probes",
    "wrapper health",
    "wrapper commands",
    "wrapper phases",
    "wrapper refresh",
    "wrapper intent",
    "wrapper reboot",
    *TICK_GROUPS,
    "wrapper relay",
    "wrapper view",
    "wrapper events",
    "wrapper metrics",
]

Get = Callable[[str], Any]
Graph = tuple[tuple[Step, ...], Context, tuple[engine.StepRecord, ...]]


@dataclass(frozen=True)
class _Marker:
    name: str
    group: str
    call: Callable[[Get], list[str]] = field(default=lambda get: [])
    lane: Lane = Lane("tick")
    budget_s: float = 5.0
    interval_s: float = 0.0
    needs: frozenset[str] = frozenset()
    reads: frozenset[Domain] = frozenset()
    effects: frozenset[str] = frozenset()
    feature: str = ""

    @property
    def writes(self) -> frozenset[Domain]:
        return frozenset({actions_of(self.name)})

    def run(self, ctx: Context) -> Result:
        found = self.call(partial(ctx.view.read, PASS))
        return Result((Write(actions_of(self.name), self.name, found),))


def _graphs(monkeypatch: pytest.MonkeyPatch) -> list[Graph]:
    seen: list[Graph] = []
    real = engine.run_graph

    def run_graph(steps: Any, ctx: Context, *args: Any, **options: Any) -> tuple[engine.StepRecord, ...]:
        found = tuple(steps)
        records = real(found, ctx, *args, **options)
        seen.append((found, ctx, records))
        return records

    monkeypatch.setattr(engine, "run_graph", run_graph)
    return seen


def _groups(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Context]]:
    seen: list[tuple[str, Context]] = []
    real = engine.run_group

    def run_group(name: str, ctx: Context, **options: Any) -> tuple[engine.StepRecord, ...]:
        seen.append((name, ctx))
        return real(name, ctx, **options)

    monkeypatch.setattr(engine, "run_group", run_group)
    return seen


def _with(monkeypatch: pytest.MonkeyPatch, *extra: Step, real: bool = True) -> None:
    fresh = registry.Registry()
    for step in (*(registry.steps() if real else ()), *extra):
        fresh.register(step)
    monkeypatch.setattr(registry, "registry", fresh)


def _ran(graph: Graph) -> list[str]:
    steps, _, records = graph
    group = {step.name: step.group for step in steps}
    found: list[str] = []
    for record in records:
        if not record.rested and group[record.name] not in found:
            found.append(group[record.name])
    return found


def _present(graph: Graph, sites: list[str]) -> list[str]:
    held = {step.group for step in graph[0]}
    return [site for site in sites if site in held]


def _recorder(into: list[Any], value: Callable[[Get], Any], out: list[str] | None = None) -> Callable[[Get], list[str]]:
    def call(get: Get) -> list[str]:
        into.append(value(get))
        return list(out or [])

    return call


def test_a_tick_pass_runs_its_sites_in_order(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    seen = _graphs(monkeypatch)
    tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), now_ms=1_000)
    [graph] = seen
    assert sites.TICK_SITES == tuple(TICK_GROUPS)
    assert _ran(graph) == _present(graph, TICK_GROUPS)


def test_each_tick_site_needs_every_step_of_the_site_before(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    seen = _graphs(monkeypatch)
    tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), now_ms=1_000)
    [(steps, _, records)] = seen
    late = registry._late(steps)
    members = {site: [s for s in steps if s.group == site and s.name not in late] for site in TICK_GROUPS}
    filled = [site for site in TICK_GROUPS if members[site]]
    assert len(filled) > 2
    order = [record.name for record in records]
    crossings = 0
    for before, after in zip(filled, filled[1:], strict=False):
        for step in steps:
            if step.group == after:
                crossed = {other.name for other in members[before] if registry.crossed(other, step)}
                crossings += len(crossed)
                assert crossed <= step.needs, (before, step.name)
                assert all(order.index(name) < order.index(step.name) for name in crossed)
    assert crossings > 0


def test_the_tick_graph_reads_its_pass_state_under_the_group_budget(
    store: RedisStore,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _graphs(monkeypatch)
    started = monotonic()
    tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), now_ms=1_000)
    [(_, ctx, _)] = seen
    assert ctx.view.read(PASS, "slug") == "sw"
    assert ctx.view.read(PASS, "now_ms") == 1_000
    assert ctx.view.read(PASS, "store") is store
    assert ctx.view.read(Domain("tasks"), "slug") is None
    config: Any = ctx.view.read(PASS, "config")
    rows: Any = ctx.view.read(PASS, "rows")
    assert config.state == "running"
    assert set(rows) == {"t1"}
    assert ctx.deadline.expires_at - started == pytest.approx(tick_module.GROUP_BUDGET_S, abs=5.0)
    assert tick_module.GROUP_BUDGET_S == 600.0


def test_a_stopped_swarm_runs_only_the_start_site(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    store.update("sw", state="stopped")
    seen = _graphs(monkeypatch)
    tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), now_ms=1_000)
    [graph] = seen
    assert _ran(graph) == ["start"]


def test_a_registered_step_runs_in_its_tick_site(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    ran: list[str] = []
    _with(monkeypatch, *observe.STEPS, _Marker("marker", "agents", _recorder(ran, lambda get: "agents")), real=False)
    tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), now_ms=1_000)
    assert ran == ["agents"]


def test_the_tick_drops_its_actions_when_a_value_it_reads_was_left(
    store: RedisStore,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []
    _with(monkeypatch, _Marker("marker", "start", _recorder(ran, lambda get: "start", ["acted"])))
    monkeypatch.setattr(sites, "TICK_KEYS", frozenset({"config", "doc", "rows", "never stored"}))
    assert tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), now_ms=1_000) == []
    assert ran == ["start"]


def test_the_tick_wrapper_runs_one_graph_between_its_before_and_closing_groups(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    groups = _groups(monkeypatch)
    seen = _graphs(monkeypatch)
    cli.run_tick(swarm, "sw", ledger, rt, FakeHerdr({}))
    assert [name for name, _ in groups] == ["before", "closing"]
    [graph] = seen
    assert cli.PASS_SITES == tuple(WRAPPER_GROUPS)
    ran = _ran(graph)
    assert ran.count("wrapper intent") == 1
    expected = [site for site in _present(graph, WRAPPER_GROUPS) if site != "wrapper intent"]
    assert [site for site in ran if site != "wrapper intent"] == expected


def test_the_intent_site_joins_the_wrapper_at_the_relay(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    seen = _graphs(monkeypatch)
    cli.run_tick(swarm, "sw", ledger, rt, FakeHerdr({}))
    [(steps, _, _)] = seen
    intent = [step for step in steps if step.group == "wrapper intent"]
    assert intent
    for step in steps:
        if step.group == "wrapper relay":
            assert {other.name for other in intent if registry.crossed(other, step)} <= step.needs
        elif step.group in ("wrapper reboot", "start"):
            assert not {other.name for other in intent} & step.needs
    assert cli.INTENT_JOINS == {"wrapper intent": "wrapper relay"}


@pytest.mark.feature("swarm-tick-declared-effects")
def test_the_wrapper_builds_the_real_tools_once_per_pass_for_every_group(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    groups = _groups(monkeypatch)
    seen = _graphs(monkeypatch)
    cli.run_tick(swarm, "sw", ledger, rt, FakeHerdr({}))
    tools = groups[0][1].tools
    assert all(ctx.tools is tools for _, ctx in groups)
    assert all(ctx.tools is tools for _, ctx, _ in seen)
    assert isinstance(tools, Tools)
    assert isinstance(tools.inbox, StoreInbox)
    cli.run_tick(swarm, "sw", ledger, rt, FakeHerdr({}))
    assert groups[-1][1].tools is not tools


def test_the_wrapper_hands_metrics_the_action_count_and_after_the_reread_doc(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    groups = _groups(monkeypatch)
    seen = _graphs(monkeypatch)
    states: list[dict[str, Any]] = []
    real = ledger.state

    def state(slug: str) -> dict[str, Any]:
        states.append(dict(real(slug)))
        return states[-1]

    monkeypatch.setattr(ledger, "state", state)
    cli.run_tick(swarm, "sw", ledger, rt, FakeHerdr({}))
    [(_, ctx, _)] = seen
    closing = dict(groups)["closing"]
    assert isinstance(ctx.view.read(PASS, "count"), int)
    assert closing.view.read(PASS, "doc") is states[-1]
    assert closing.view.read(PASS, "ended") == report.FULL
    assert len(states) > 1


def test_the_wrapper_steps_after_fencing_read_the_fenced_runtime(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    probed: list[Any] = []
    fenced: list[Any] = []
    _with(
        monkeypatch,
        _Marker("probe marker", "wrapper probes", _recorder(probed, lambda get: get("runtime"))),
        *(
            _Marker(f"{site} marker", site, _recorder(fenced, lambda get: get("runtime")))
            for site in ("wrapper health", "wrapper commands", "wrapper reboot", "wrapper events", "wrapper metrics")
        ),
    )
    cli.run_tick(swarm, "sw", ledger, rt, FakeHerdr({}))
    assert probed == [rt]
    assert len(fenced) == 5
    assert all(isinstance(runtime, controller.FencedRuntime) for runtime in fenced)


def test_the_wrapper_runs_the_tick_with_the_fenced_pass_state_and_keeps_its_actions(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    calls: list[tuple[Any, ...]] = []
    keys = ("slug", "store", "ledger", "runtime", "now_ms")
    _with(
        monkeypatch,
        _Marker("tick marker", "start", _recorder(calls, lambda get: tuple(map(get, keys)), ["tick acted"])),
    )
    monkeypatch.setattr(cli, "now_ms", lambda: 9_000_000)
    monkeypatch.setattr(reboot, "boot_pass", lambda *args: ["rebooted"])
    found = cli.run_tick(swarm, "sw", ledger, rt, FakeHerdr({}))
    assert found.index("rebooted") < found.index("tick acted")
    [(slug, held, used, runtime, now)] = calls
    assert (slug, held, used, now) == ("sw", swarm, ledger, 9_000_000)
    assert isinstance(runtime, controller.FencedRuntime)


def test_a_failed_reboot_pass_raises_before_the_tick_runs(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    calls: list[str] = []
    _with(monkeypatch, _Marker("tick marker", "start", _recorder(calls, lambda get: "start")))

    def failed(*args: Any) -> list[str]:
        raise RuntimeError("reboot failed")

    monkeypatch.setattr(reboot, "boot_pass", failed)
    with pytest.raises(RuntimeError, match="^reboot failed$"):
        cli.run_tick(swarm, "sw", ledger, rt, FakeHerdr({}))
    assert calls == []


def _raise(message: str) -> Callable[[Get], list[str]]:
    def call(get: Get) -> list[str]:
        get("box")[task_steps.RAISED] = RuntimeError(message)
        return []

    return call


def test_a_failed_tick_step_raises_out_of_the_tick_wrapper(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    relayed: list[str] = []
    _with(
        monkeypatch,
        _Marker("failing marker", "agents", _raise("tick failed")),
        _Marker("relay marker", "wrapper relay", _recorder(relayed, lambda get: "relay")),
    )
    groups = _groups(monkeypatch)
    with pytest.raises(RuntimeError, match="^tick failed$"):
        cli.run_tick(swarm, "sw", ledger, rt, FakeHerdr({}))
    assert relayed == []
    assert [name for name, _ in groups] == ["before", "closing"]


def test_a_tick_value_left_drops_the_tick_actions_and_the_wrapper_goes_on(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    later: list[str] = []
    _with(
        monkeypatch,
        _Marker("tick marker", "start", lambda get: ["tick acted"]),
        *(
            _Marker(f"{site} marker", site, _recorder(later, lambda get, site=site: site))
            for site in ("wrapper relay", "wrapper view", "wrapper events", "wrapper metrics")
        ),
    )
    groups = _groups(monkeypatch)
    monkeypatch.setattr(sites, "TICK_KEYS", frozenset({"config", "doc", "rows", "never stored"}))
    assert cli.run_tick(swarm, "sw", ledger, rt, FakeHerdr({})) == []
    assert later == ["wrapper relay", "wrapper view", "wrapper events", "wrapper metrics"]
    assert [name for name, _ in groups][-1] == "closing"


def test_a_failed_wrapper_step_raises_out_of_the_tick_wrapper(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    phased: list[str] = []
    _with(monkeypatch, _Marker("phase marker", "wrapper phases", _recorder(phased, lambda get: "phases")))
    groups = _groups(monkeypatch)

    def consume(*args: object) -> list[str]:
        raise RuntimeError("consume failed")

    monkeypatch.setattr(command_runner, "consume", consume)
    with pytest.raises(RuntimeError, match="^consume failed$"):
        cli.run_tick(swarm, "sw", ledger, rt, FakeHerdr({}))
    assert phased == []
    assert [name for name, _ in groups] == ["before", "closing"]
    assert dict(groups)["closing"].view.read(PASS, "ended") is None


def test_a_snapshot_job_left_past_its_budget_ends_the_pass_full_without_its_line(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    groups = _groups(monkeypatch)
    release = Event()
    step = next(step for step in report.STEPS if step.name == "snapshot job")
    monkeypatch.setattr(step, "floor_s", 0.05)

    called: list[bool] = []

    def start(*args: object) -> bool:
        called.append(True)
        release.wait(60.0)
        return False

    monkeypatch.setattr(snapshot_job, "start", start)
    try:
        found = cli.run_tick(swarm, "sw", ledger, rt, FakeHerdr({}))
        assert not any(line.startswith("pass ended early") for line in found)
        assert "started automatic snapshot" not in found
        assert swarm.redis.get(swarm.key("sw", "last-tick")) is not None
        assert swarm.redis.get(swarm.key("sw", "early-end-streak")) is None
    finally:
        release.set()
        running = engine._RUNNING.get(("sw", "snapshot job"))
        if running is not None:
            running.join(30.0)
    assert called == [True]
    assert [name for name, _ in groups][-1] == "closing"
    assert dict(groups)["closing"].view.read(PASS, "ended") == report.FULL


def test_the_tick_hands_the_wake_pass_its_clock_window_and_quiet_window(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    calls = []
    monkeypatch.setattr(cli, "now_ms", lambda: 9_000_000)
    monkeypatch.setenv(wake.WINDOW_ENV, "60")
    monkeypatch.setattr(wake, "maintenance", lambda *args: calls.append(args[-2:]) or [])
    monkeypatch.setattr(wake, "wake_pass", lambda *args: pytest.fail("tick swept the wake ladder"))
    cli.run_tick(swarm, "sw", ledger, rt, FakeHerdr({}))
    assert calls == [(9_000_000, 60_000)]
