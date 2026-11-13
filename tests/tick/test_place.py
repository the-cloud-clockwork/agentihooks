"""The placement steps run the tick's hand written acks, ranking, preempt and spawn calls at their sites in today's order."""

import runpy
from contextlib import contextmanager, nullcontext
from types import MappingProxyType, SimpleNamespace
from typing import Any, Iterator

import pytest

import scripts.swarm.cli  # noqa: F401
from scripts.swarm import bottleneck_remedy, panic
from scripts.tick import engine, registry
from scripts.tick.adapters.calls import LedgerCalls, LocalMetrics, SpawnRuntimeCalls, SpawnStoreCalls
from scripts.tick.adapters.fakes import fake_tools
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
from scripts.tick.steps import (
    AGENTS,
    PASS,
    SWARM,
    TASKS,
    CallStep,
    actions,
    actions_of,
    budgets,
    pass_domains,
    place,
    tasks,
)

BOXED = ("config", "rows", "doc", "sleeping")

pytestmark = pytest.mark.unit


class _State:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state

    def read(self, domain: Domain, key: str) -> object | None:
        return self.state.get(key) if domain == PASS else None


LOCK_KEY = "sw:acks-lock"


class _Redis:
    def __init__(self, log: list[Any]) -> None:
        self.log = log
        self.token = ""

    def delete(self, key: str) -> None:
        if key == LOCK_KEY:
            self.token = ""
        else:
            self.log.append(("delete", key))

    def set(self, key: str, value: str, nx: bool, px: int) -> bool:
        assert (key, nx, px) == (LOCK_KEY, True, 30_000)
        if self.log.count("acks held"):
            return False
        self.token = value
        return True

    def get(self, key: str) -> str:
        assert key == LOCK_KEY
        return self.token


def _store(log: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(redis=_Redis(log), key=lambda slug, name: f"{slug}:{name}")


ROWS = {"t1": {"id": "t1", "state": "open"}}


def _probe(runtime: Any, *args: Any) -> None:
    return None


def _pass(log: list[Any], state: str = "running", engine_name: str = "tick", **extra: Any) -> dict[str, Any]:
    return {
        "slug": "sw",
        "config": SimpleNamespace(state=state, engine=engine_name),
        "store": _store(log),
        "ledger": "ledger",
        "runtime": "runtime",
        "rows": ROWS,
        "doc": "doc",
        "now_ms": 7,
        "due": "due key",
        "box": {},
        "sleeping": False,
    } | extra


def _tick(log: list[Any], places: bool = True) -> SimpleNamespace:
    def skip_refused(function: Any, *args: Any) -> list[str]:
        log.append((function, *args, *([place.PASS_DEADLINE.get()] if function == "spawn" else [])))
        return [f"ran {function if isinstance(function, str) else function.__name__}"]

    @contextmanager
    def placement_pass(runtime: Any) -> Iterator[None]:
        log.append(("enter pass", runtime))
        yield
        log.append(("leave pass", runtime))

    class _Seats:
        def __init__(self, *args: Any) -> None:
            self.args = args

        def __eq__(self, other: object) -> bool:
            return isinstance(other, _Seats) and self.args == other.args

    return SimpleNamespace(
        skip_refused=skip_refused,
        PLACING=nullcontext(),
        EVENTS_ENGINE="events",
        _placement_pass=placement_pass,
        _spawn="spawn",
        SPAWN_BUDGET_S=60,
        _collect_acks="collect acks",
        _codex_hook_order="codex hook order",
        _ended="ended",
        _current_owner="owner",
        ready_in_order="ready in order",
        claim_view="claim view",
        readiness=SimpleNamespace(tick_launches=lambda engine_name: log.append(("places", engine_name)) or places),
        preempt=SimpleNamespace(probe=_probe, Seats=_Seats, run="preempt"),
        bottleneck_remedy=bottleneck_remedy,
        panic=panic,
    )


def _run(
    group: str, tick: SimpleNamespace, state: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tools: Any = None
) -> list[str]:
    monkeypatch.setattr("scripts.tick.steps.import_module", lambda name: tick)
    ctx = Context(_State(state), Deadline.after(60.0), tools or fake_tools())
    return actions(engine.run_group(group, ctx))


def _store_writes(tools: Any) -> list[object]:
    return [call.args[0] for call in tools.coordination.calls if call.operation == "store_write"]


@pytest.fixture
def calm(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    log: list[Any] = []
    monkeypatch.setattr(panic, "state", lambda store: log.append("panic read") and None)
    monkeypatch.setattr(bottleneck_remedy, "step", "bottleneck remedy")
    return log


def test_the_placement_steps_sit_at_their_sites_in_legacy_order() -> None:
    assert (place.ACKS, place.PLACEMENT) == ("placement acks", "placement")
    assert [step.name for step in engine.group_steps(place.ACKS)] == ["collect acks", "codex hook order"]
    assert [step.name for step in engine.group_steps(place.PLACEMENT)] == [
        "rank pass",
        "bottleneck remedy",
        "preempt",
        "spawn",
    ]
    declared = {
        step.name: (step.needs, step.reads - pass_domains(*BOXED), step.writes - {actions_of(step.name)})
        for step in place.STEPS
    }
    assert declared["preempt"] == (place.PREEMPT_NEEDS, frozenset({AGENTS}), frozenset({AGENTS}))
    assert declared["spawn"] == (
        budgets.SPAWN_NEEDS,
        frozenset({AGENTS, TASKS, SWARM}),
        frozenset({AGENTS, TASKS, Domain("pass:rows")}),
    )
    assert declared["rank pass"] == (frozenset({"drained resume"}), frozenset(), frozenset({tasks.RANK}))
    assert {name: value[1:] for name, value in declared.items() if name not in {"preempt", "spawn", "rank pass"}} == {
        name: (frozenset(), frozenset())
        for name in (
            "quota notice",
            "lost runner",
            "time left",
            "collect acks",
            "codex hook order",
            "bottleneck remedy",
        )
    }
    assert [step.name for step in engine.group_steps(place.NOTICES)] == ["quota notice", "lost runner", "time left"]
    registered = [step for wave in registry.waves() for step in wave]
    for step in place.STEPS:
        assert step in registered
        lane = engine.SLOW if step.name in {"lost runner", "time left"} else CallStep.lane
        assert (step.lane, step.budget_s) == (lane, budgets.timing_of(step.name)[0])


def test_placement_requires_the_report_steps_its_spawn_step_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    required: list[str] = []
    monkeypatch.setattr(registry, "registry", registry.Registry())
    monkeypatch.setattr(registry, "require", required.append)
    runpy.run_module("scripts.tick.steps.place")
    assert required == ["scripts.tick.steps.report"]
    assert {"overrun pass", "quiet pass"} <= budgets.SPAWN_NEEDS


def test_the_placement_notices_run_their_calls_with_the_pass_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import lost_runner, quota_notice, time_left

    calls: list[tuple[Any, ...]] = []

    def skip_refused(function: Any, *args: Any) -> list[str]:
        calls.append((function, *args))
        return [f"ran {len(calls)}"]

    state = {
        "slug": "sw",
        "config": "config",
        "store": "store",
        "ledger": "ledger",
        "runtime": "runtime",
        "doc": "doc",
        "now_ms": 7,
        "box": {},
    }
    assert _run(
        place.NOTICES, SimpleNamespace(skip_refused=skip_refused, PLACING=nullcontext()), state, monkeypatch
    ) == [
        "ran 1",
        "ran 2",
        "ran 3",
    ]
    assert calls == [
        (quota_notice.refresh, "sw", "config", "store", "ledger", "runtime", 7),
        (lost_runner.sweep, "sw", "config", "store", 7),
        (time_left.refresh, "sw", "store", "ledger", "runtime", "config", "doc"),
    ]


def test_collect_acks_then_the_codex_hook_order_while_awake(calm: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    state = _pass(calm)
    found = _run(place.ACKS, _tick(calm), state, monkeypatch)
    assert found == ["ran collect acks", "ran codex hook order"]
    assert calm == [
        ("collect acks", "sw", state["config"], state["store"], "ledger", "runtime", "doc", 7),
        ("codex hook order",),
    ]


def test_the_pass_skips_collecting_acks_while_the_wait_pass_holds_the_acks_lock(
    calm: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calm.append("acks held")
    found = _run(place.ACKS, _tick(calm), _pass(calm), monkeypatch)
    assert found == ["ran codex hook order"]
    assert calm == ["acks held", ("codex hook order",)]


def test_a_sleeping_swarm_collects_acks_without_the_codex_hook_order(
    calm: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    found = _run(place.ACKS, _tick(calm), _pass(calm, sleeping=True), monkeypatch)
    assert found == ["ran collect acks"]
    assert [entry[0] for entry in calm] == ["collect acks"]


def test_a_running_pass_ranks_remedies_preempts_and_spawns(calm: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    tick = _tick(calm)
    state = _pass(calm)
    config, store = state["config"], state["store"]
    found = _run(place.PLACEMENT, tick, state, monkeypatch)
    assert found == ["ran rank_writes", "ran bottleneck remedy", "ran preempt", "ran spawn"]
    seats = calm[3][-1]
    assert seats.args[:4] == (ROWS, "ended", "owner", "ready in order")
    assert seats.args[4].func is _probe
    assert seats.args[4].args == ("runtime",)
    assert seats.args[5] == "claim view"
    assert calm == [
        (tasks.rank_writes, tick, "sw", config, store, "ledger", "doc", 7),
        ("bottleneck remedy", "sw", store, "ledger", "doc", 7),
        "panic read",
        ("preempt", "sw", config, store, "doc", 7, seats),
        ("places", "tick"),
        "panic read",
        ("enter pass", calm[7][5]),
        ("spawn", "sw", config, calm[7][3], calm[7][4], calm[7][5], ROWS, "doc", 7, calm[7][-1]),
        ("leave pass", calm[7][5]),
    ]
    assert isinstance(calm[7][-1], Deadline)
    assert isinstance(calm[7][3], SpawnStoreCalls)
    assert calm[7][3].deadline is calm[7][4].deadline
    assert isinstance(calm[7][4], LedgerCalls)
    assert isinstance(calm[7][5], SpawnRuntimeCalls)
    assert calm[7][5].deadline is calm[7][4].deadline
    assert calm[7][4].deadline.expires_at == calm[7][-1].expires_at + tick.SPAWN_BUDGET_S
    spawned = calm[7][6]
    assert spawned is not ROWS and state["box"]["rows"] == ROWS
    assert isinstance(state["box"]["rows"], MappingProxyType)
    assert state["box"]["rows"]["t1"] is spawned["t1"]


@pytest.mark.parametrize(("state", "engine_name"), [("paused", "tick"), ("running", "events")])
def test_preempt_waits_for_a_running_tick_engine(
    calm: list[Any], monkeypatch: pytest.MonkeyPatch, state: str, engine_name: str
) -> None:
    found = _run(place.PLACEMENT, _tick(calm, places=False), _pass(calm, state, engine_name), monkeypatch)
    assert found == ["ran rank_writes", "ran bottleneck remedy"]
    assert "preempt" not in [entry[0] for entry in calm if isinstance(entry, tuple)]


def test_a_panic_holds_preempt_and_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[Any] = []
    monkeypatch.setattr(panic, "state", lambda store: "halted")
    monkeypatch.setattr(bottleneck_remedy, "step", "bottleneck remedy")
    found = _run(place.PLACEMENT, _tick(log), _pass(log), monkeypatch)
    assert found == ["ran rank_writes", "ran bottleneck remedy"]
    assert log[-1] == ("places", "tick")


def test_spawn_waits_until_the_tick_places(calm: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    tools = fake_tools()
    found = _run(place.PLACEMENT, _tick(calm, places=False), _pass(calm), monkeypatch, tools)
    assert found == ["ran rank_writes", "ran bottleneck remedy", "ran preempt"]
    assert _store_writes(tools) == []


def test_a_paused_swarm_does_not_spawn(calm: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    tools = fake_tools()
    found = _run(place.PLACEMENT, _tick(calm), _pass(calm, "paused"), monkeypatch, tools)
    assert found == ["ran rank_writes", "ran bottleneck remedy"]
    assert _store_writes(tools) == []


def test_a_spawning_pass_clears_its_due_mark_through_the_coordination_port(
    calm: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = fake_tools()
    _run(place.PLACEMENT, _tick(calm), _pass(calm), monkeypatch, tools)
    assert _store_writes(tools) == ["redis.delete"]


def test_a_held_exception_skips_every_placement_step(calm: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    state = _pass(calm, box={tasks.RAISED: RuntimeError("earlier")})
    assert _run(place.PLACEMENT, _tick(calm), state, monkeypatch) == []
    assert calm == []


SPAWN_CLUSTER = (
    "_spawn",
    "_Pass",
    "_pass_reclaim",
    "Launch",
    "_prepare",
    "_launch",
    "_launch_all",
    "_retry_successor",
    "_reported",
    "_spawn_order",
    "_ranked",
    "_block_unresolved",
    "_unresolved",
    "_lives_spent",
    "_claim_cap",
    "_free_seat",
    "primed",
    "placed_record",
    "_LaunchClock",
    "_launch_room",
    "_steer",
    "_steered_off",
    "_record_spawn_failure",
    "_owner_hold",
    "_placement_pass",
    "_spawn_stop",
    "_host_full",
    "host_spent",
    "_spend_host",
    "_spend_local",
    "_release_hive_launch",
    "_hold",
    "spawn_holds",
    "_held_for_master",
)


@pytest.mark.feature("swarm-tick-spawn")
@pytest.mark.parametrize("name", SPAWN_CLUSTER)
def test_tick_reexports_the_moved_spawn_name(name: str) -> None:
    from scripts.swarm import tick

    moved = getattr(place, name)
    assert moved.__module__ == "scripts.tick.steps.place"
    assert getattr(tick, name) is moved


@pytest.mark.feature("swarm-tick-spawn")
def test_the_tick_module_holds_no_spawn_definition() -> None:
    from pathlib import Path

    from scripts.swarm import tick

    text = Path(str(tick.__file__)).read_text(encoding="utf-8")
    defined = [
        name
        for name in SPAWN_CLUSTER
        if any(f"\n{kind} {name}{end}" in text for kind in ("def", "class") for end in "(:")
    ]
    assert defined == []


@pytest.mark.feature("swarm-tick-spawn")
def test_the_spawn_step_declares_its_ports_feature_and_lane() -> None:
    spawn = next(step for step in place.STEPS if step.name == "spawn")
    assert spawn.effects == frozenset({"ledger", "runtime", "coordination", "inbox", "metrics"})
    assert (spawn.feature, spawn.lane, spawn.group) == ("swarm-tick-spawn", CallStep.lane, place.PLACEMENT)


@pytest.mark.feature("swarm-tick-spawn")
@pytest.mark.parametrize(("owned", "expected"), [({"t1"}, "task t1 retains its live owner"), ({"t2"}, "")])
def test_moved_spawn_code_reaches_tick_seams_through_the_tick_module(
    monkeypatch: pytest.MonkeyPatch, owned: set[str], expected: str
) -> None:
    from scripts.swarm import tick

    released: list[tuple[str, str, dict[str, str]]] = []
    monkeypatch.setattr(tick, "_owned_tasks", lambda slug, store: owned if (slug, store) == ("sw", "store") else set())
    monkeypatch.setattr(tick, "_release_admission", lambda slug, runtime, task: released.append((slug, runtime, task)))
    task = {"id": "t1"}
    assert place._owner_hold("sw", "store", "runtime", task) == expected
    assert released == ([("sw", "runtime", task)] if expected else [])


@pytest.mark.feature("swarm-tick-spawn")
def test_a_free_seat_comes_from_the_tick_seat_address(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import tick

    monkeypatch.setattr(tick, "seat_address", lambda slug, label: f"{label}@{slug}")
    assert place._free_seat("sw", "eng", {"eng-1@sw"}, "") == "eng-2@sw"
    assert place._free_seat("sw", "eng", {"eng-1@sw"}, "eng-1@sw") == "eng-2@sw"
    assert place._free_seat("sw", "eng", {"eng-1@sw"}, "eng-7@sw") == "eng-7@sw"


@pytest.mark.feature("swarm-tick-spawn")
def test_a_free_seat_looks_no_further_than_one_seat_past_the_taken_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import tick

    monkeypatch.setattr(tick, "seat_address", lambda slug, label: "x" if label in ("eng-1", "eng-2") else label)
    with pytest.raises(StopIteration):
        place._free_seat("sw", "eng", {"x"}, "")


@pytest.mark.feature("swarm-tick-spawn")
def test_the_pass_reclaim_reads_remote_heads_once_and_binds_them(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import tick

    calls: list[str] = []
    monkeypatch.setattr(tick, "remote_heads", lambda repo: calls.append(repo) or {"repo": repo})
    monkeypatch.setattr(tick, "reclaim", lambda *args, **kwargs: (args, kwargs))
    run = SimpleNamespace(memo={}, config=SimpleNamespace(repo="org/repo"))
    first = place._pass_reclaim(run)
    second = place._pass_reclaim(run)
    assert first.func is tick.reclaim
    assert first.keywords == {"heads": {"repo": "org/repo"}}
    assert second.keywords == {"heads": {"repo": "org/repo"}}
    assert first("task") == (("task",), {"heads": {"repo": "org/repo"}})
    assert run.memo == {"heads": {"repo": "org/repo"}}
    assert calls == ["org/repo"]


@pytest.mark.feature("swarm-tick-spawn")
def test_the_pass_reclaim_reuses_heads_the_pass_already_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import tick

    monkeypatch.setattr(tick, "remote_heads", lambda repo: pytest.fail("heads read twice"))
    run = SimpleNamespace(memo={"heads": {"cached": "h"}}, config=SimpleNamespace(repo="org/repo"))
    assert place._pass_reclaim(run).keywords == {"heads": {"cached": "h"}}


@pytest.mark.feature("swarm-tick-spawn")
def test_a_legacy_launch_is_admitted_with_the_pass_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import tick
    from scripts.swarm_v2.runtime import codex

    admitted: list[tuple[Any, ...]] = []
    placed = SimpleNamespace(launch_job=None)
    runtime = SimpleNamespace(spawn=lambda config, lane, name, primed: placed)
    monkeypatch.setattr(tick, "placed_record", lambda record, where: ("placed", record, where))
    monkeypatch.setattr(codex, "admit_legacy", lambda *args: admitted.append(args) or "admitted")
    record = SimpleNamespace(lane="eng", name="eng@1")
    run = SimpleNamespace(runtime=runtime, config="config", store="store", slug="sw")
    launch = SimpleNamespace(record=record, primed={})
    assert place._launch(run, launch) == (placed, "admitted")
    [(store, slug, agent, given, table)] = admitted
    assert (store, slug, agent, given) == ("store", "sw", ("placed", record, placed), runtime)
    assert (type(table.__self__), table.__func__) == (LocalMetrics, LocalMetrics.table)


class _ReadyRead(Exception):
    """Stops the spawn order once it has asked the tick for the ready tasks of a lane."""


@pytest.mark.feature("swarm-tick-spawn")
def test_the_spawn_order_reads_ready_tasks_with_the_pass_rows_doc_and_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import capacity, tick

    asked: list[tuple[Any, ...]] = []

    def ready(*args: Any) -> list[Any]:
        asked.append(args)
        raise _ReadyRead

    monkeypatch.setattr(capacity, "read", lambda store, slug: "decision")
    monkeypatch.setattr(capacity, "caps", lambda decision, config: {"eng": 1})
    monkeypatch.setattr(place.dispatcher, "claim_key", lambda config, doc: "key")
    monkeypatch.setattr(place.preempt, "yielding", lambda store, slug, doc, key: "dispatched")
    monkeypatch.setattr(tick, "claim_view", lambda slug, store, rows, doc, dispatched: "claims")
    monkeypatch.setattr(tick, "_running", lambda agents, rows, lane: [])
    monkeypatch.setattr(tick, "ready_by_lane", ready)
    rows, doc = {"t1": {"id": "t1"}}, {"tasks": {}}
    config: Any = SimpleNamespace()
    store: Any = "store"
    with pytest.raises(_ReadyRead):
        place._spawn_order("sw", config, store, [], rows, doc, SimpleNamespace())
    assert asked == [("sw", "store", rows, doc, ["eng"], "dispatched", "claims")]


SPAWN_LEFT = "spawn stopped at the pass deadline, {left} of {total} ready tasks left for the next pass"


class _Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: float) -> None:
        """Start the clock at ``now``."""
        self.now = now

    def __call__(self) -> float:
        """Return the current reading."""
        return self.now


class _SpawnClock:
    """A launch clock that never spends the launch budget."""

    failing = ""

    def spent(self, task: dict[str, Any], batch: list[Any]) -> None:
        """Report the launch budget unspent."""
        return None

    def prepared(self, at: float) -> None:
        """Ignore the preparation time."""

    def charge(self, at: float, errors: list[Exception], launches: int) -> None:
        """Ignore the launch time."""


def _spawn_pass(monkeypatch: pytest.MonkeyPatch, clock: _Clock, step_s: float, ids: list[str]) -> list[str]:
    """Make the spawn pass see one ready task per id, each preparation spending ``step_s`` of ``clock``."""
    from scripts.swarm import tick

    prepared: list[str] = []

    def prepare(run: Any, lane: str, task: dict[str, Any], *rest: Any) -> tuple[Any, list[str]]:
        """Record the task and spend ``step_s`` of the clock."""
        prepared.append(task["id"])
        clock.now += step_s
        return SimpleNamespace(record=SimpleNamespace(name=task["id"])), []

    def launch_all(run: Any, launch_clock: Any, batch: list[Any]) -> list[str]:
        """Name the batch launched."""
        return [f"launched {' '.join(launch.record.name for launch in batch)}"] if batch else []

    order = [("eng", {"id": task_id}) for task_id in ids]
    monkeypatch.setattr(tick, "_held_for_master", lambda slug, store, now_ms: [])
    monkeypatch.setattr(tick, "_spawn_order", lambda *args: (order, []))
    monkeypatch.setattr(tick, "_block_unresolved", lambda *args: [])
    monkeypatch.setattr(tick, "_LaunchClock", _SpawnClock)
    monkeypatch.setattr(tick, "_spawn_stop", lambda *args: "")
    monkeypatch.setattr(tick, "_launch_room", lambda runtime, config: len(ids) + 1)
    monkeypatch.setattr(tick, "_prepare", prepare)
    monkeypatch.setattr(tick, "_launch_all", launch_all)
    return prepared


def _spawn_with(deadline: Deadline | None) -> list[str]:
    """Run the spawn pass of swarm sw with ``deadline``."""
    store: Any = SimpleNamespace(agents=lambda slug: [])
    config: Any = SimpleNamespace()
    token = place.PASS_DEADLINE.set(deadline)
    try:
        return place._spawn("sw", config, store, "ledger", "runtime", {}, {}, 7)
    finally:
        place.PASS_DEADLINE.reset(token)


@pytest.mark.feature("swarm-tick-spawn")
def test_a_spawn_pass_past_its_deadline_stops_and_leaves_every_task_for_the_next_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(100.0)
    prepared = _spawn_pass(monkeypatch, clock, 0.0, ["t1", "t2"])

    actions = _spawn_with(Deadline(100.0, clock))

    assert prepared == []
    assert actions == [SPAWN_LEFT.format(left=2, total=2)]


@pytest.mark.feature("swarm-tick-spawn")
def test_an_expired_spawn_pass_never_blocks_unresolved_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import tick

    clock = _Clock(100.0)
    _spawn_pass(monkeypatch, clock, 0.0, ["t1", "t2"])

    def block(*args: Any) -> list[str]:
        pytest.fail("expired spawn pass reached unresolved task writes")

    monkeypatch.setattr(tick, "_block_unresolved", block)

    assert _spawn_with(Deadline(100.0, clock)) == [SPAWN_LEFT.format(left=2, total=2)]


@pytest.mark.feature("swarm-tick-spawn")
def test_a_spawn_pass_whose_deadline_expires_midway_launches_what_it_prepared_and_leaves_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(0.0)
    prepared = _spawn_pass(monkeypatch, clock, 5.0, ["t1", "t2", "t3"])

    actions = _spawn_with(Deadline(4.0, clock))

    assert prepared == ["t1"]
    assert actions == ["launched t1", SPAWN_LEFT.format(left=2, total=3)]


@pytest.mark.feature("swarm-tick-spawn")
@pytest.mark.parametrize("deadline", [Deadline(10.0, _Clock(0.0)), None])
def test_a_spawn_pass_inside_its_deadline_prepares_every_task_with_no_stop_notice(
    monkeypatch: pytest.MonkeyPatch, deadline: Deadline | None
) -> None:
    prepared = _spawn_pass(monkeypatch, _Clock(0.0), 1.0, ["t1", "t2"])

    assert _spawn_with(deadline) == ["launched t1 t2"]
    assert prepared == ["t1", "t2"]


@pytest.mark.feature("swarm-tick-spawn")
def test_the_spawn_step_hands_the_spawn_pass_its_step_deadline(
    calm: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.tick.steps.import_module", lambda name: _tick(calm))
    deadline = Deadline(50.0, _Clock(0.0))
    spawn = next(step for step in place.STEPS if step.name == "spawn")

    state, tools = _pass(calm), fake_tools()

    spawn.run(Context(_State(state), deadline, tools))

    [found] = [entry for entry in calm if isinstance(entry, tuple) and entry[0] == "spawn"]
    ledger, runtime = found[4], found[5]
    store = found[3]
    assert found == ("spawn", "sw", state["config"], store, ledger, runtime, ROWS, "doc", 7, deadline)
    assert isinstance(store, SpawnStoreCalls) and store.port is tools.coordination
    assert store.deadline is ledger.deadline
    assert isinstance(ledger, LedgerCalls) and ledger.port is tools.ledger
    assert isinstance(runtime, SpawnRuntimeCalls) and runtime.port is tools.runtime
    assert runtime.deadline is ledger.deadline
    assert place.PASS_DEADLINE.get() is None
    assert (ledger.deadline.expires_at, ledger.deadline.clock) == (50.0 + _tick(calm).SPAWN_BUDGET_S, deadline.clock)
