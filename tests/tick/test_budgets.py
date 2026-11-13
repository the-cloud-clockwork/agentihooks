"""Tests for live tick step budgets: the p95 of each step's last 60 walls with headroom, never under its floor."""

import json
from contextvars import copy_context
from math import ceil
from typing import Any

import pytest

from scripts.swarm import timing
from scripts.tick import registry
from scripts.tick.steps import CallStep, budgets, place, tasks

pytestmark = pytest.mark.unit


def _store() -> Any:
    """Return a Redis store on a fresh fake Redis."""
    import fakeredis

    from scripts.swarm.store import RedisStore

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def _push(store: Any, name: str, walls: list[float]) -> None:
    """Record ``walls`` as the runs of step ``name`` through the timing step, oldest first."""
    for wall in walls:
        record = {"step": name, "budget_s": 1.0, "wall_s": wall, "phase": "finished"}
        timing.append_step(store, "sw", record)


def _resolved(store: Any, name: str) -> float:
    """Return the budget of step ``name`` after resolving the live budgets of swarm sw."""

    def run() -> float:
        budgets.resolve(store, "sw")
        return budgets.budget_of(name)

    return copy_context().run(run)


@pytest.mark.feature("swarm-tick-live-budgets")
def test_sixty_walls_give_the_p95_with_headroom_above_the_floor() -> None:
    store = _store()
    walls = [float(second) for second in range(1, 61)]
    _push(store, "reap", walls)
    assert _resolved(store, "reap") == float(ceil(57.0 * budgets.HEADROOM))
    assert _resolved(store, "reap") == 86.0


@pytest.mark.feature("swarm-tick-live-budgets")
def test_a_live_p95_under_the_floor_keeps_the_floor() -> None:
    store = _store()
    _push(store, "reap", [0.5] * 60)
    assert _resolved(store, "reap") == budgets.floor_of("reap") == 22.0


@pytest.mark.feature("swarm-tick-live-budgets")
def test_fifty_nine_walls_give_the_floor() -> None:
    store = _store()
    _push(store, "spawn", [100.0] * 59)
    assert _resolved(store, "spawn") == budgets.floor_of("spawn") == 33.0


@pytest.mark.feature("swarm-tick-live-budgets")
def test_the_window_keeps_the_last_sixty_walls() -> None:
    store = _store()
    _push(store, "exits sweep", [500.0] * 10 + [20.0] * 60)
    assert store.redis.llen(store.key("sw", "tick:walls:exits sweep")) == budgets.WINDOW == 60
    assert _resolved(store, "exits sweep") == 30.0


@pytest.mark.feature("swarm-tick-live-budgets")
def test_a_planted_floor_only_budget_goes_red(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store()
    _push(store, "night watch", [40.0] * 60)
    monkeypatch.setattr(budgets, "budget_of", budgets.floor_of)
    assert _resolved(store, "night watch") != float(ceil(40.0 * budgets.HEADROOM))


@pytest.mark.feature("swarm-tick-live-budgets")
def test_a_missing_window_is_rebuilt_from_the_steps_stream() -> None:
    store = _store()
    for wall in [12.0] * 60:
        record = {"step": "metrics record", "budget_s": 29.0, "wall_s": wall, "phase": "finished"}
        store.redis.xadd(store.key("sw", "tick:steps"), {"record": json.dumps(record)})
    store.redis.xadd(store.key("sw", "tick:steps"), {"record": json.dumps({"step": "metrics record", "wall_s": 99.0})})
    assert _resolved(store, "metrics record") == 29.0
    assert store.redis.llen(store.key("sw", "tick:walls:metrics record")) == 60
    _push(store, "metrics record", [30.0] * 60)
    assert _resolved(store, "metrics record") == 45.0


@pytest.mark.feature("swarm-tick-live-budgets")
def test_a_step_budget_reads_the_live_budget_of_its_pass() -> None:
    store = _store()
    step = {each.name: each for each in place.STEPS}["spawn"]
    _push(store, "spawn", [40.0] * 60)

    def run() -> tuple[float, float]:
        before = step.budget_s
        budgets.resolve(store, "sw")
        return before, step.budget_s

    assert copy_context().run(run) == (33.0, 60.0)


@pytest.mark.feature("swarm-tick-live-budgets")
def test_a_write_step_budget_follows_its_live_budget() -> None:
    store = _store()
    step = next(each for each in tasks.STEPS if isinstance(each, tasks.WriteStep))
    _push(store, step.name, [50.0] * 60)

    def run() -> float:
        budgets.resolve(store, "sw")
        return step.budget_s

    assert copy_context().run(run) == max(budgets.floor_of(step.name), 75.0)


@pytest.mark.feature("swarm-tick-live-budgets")
def test_floors_keep_the_six_heavy_rows_and_two_seconds_elsewhere() -> None:
    heavy = {
        "reap": 22.0,
        "spawn": 33.0,
        "stall watch": 18.0,
        "night watch": 22.0,
        "metrics record": 29.0,
        "fleet red notice": 30.0,
    }
    assert {name: budgets.floor_of(name) for name in heavy} == heavy
    for name in ("recover master", "master move fail over", "execution reconcile", "config load", "heartbeat"):
        assert budgets.floor_of(name) == 2.0
    assert not hasattr(budgets, "UNMEASURED_S")
    assert not hasattr(budgets, "MEASURED")


@pytest.mark.feature("swarm-tick-live-budgets")
def test_spawn_starts_on_floors_by_forty_seconds_whatever_the_live_budgets() -> None:
    store = _store()
    _push(store, "reap", [200.0] * 60)
    needed = [CallStep(name, "place", lambda tick, get: None) for name in sorted(budgets.SPAWN_NEEDS)]
    spawn = CallStep("spawn", "place", lambda tick, get: None, needs=budgets.SPAWN_NEEDS)

    def run() -> float:
        budgets.resolve(store, "sw")
        return registry.latest_start("spawn", [*needed, spawn])

    start = copy_context().run(run)
    assert start == 22.0
    assert start <= budgets.SPAWN_START_S


@pytest.mark.feature("swarm-tick-live-budgets")
def test_an_unknown_step_still_has_no_budget() -> None:
    with pytest.raises(ValueError) as raised:
        CallStep("never declared", "place", lambda tick, get: None)
    assert str(raised.value) == "Undeclared step timing: never declared"


@pytest.mark.feature("swarm-tick-live-budgets")
def test_a_budgeted_step_lands_on_the_steps_stream_trimmed_to_its_history(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store()
    monkeypatch.setattr(timing, "HISTORY_LIMIT", 3)
    _push(store, "reap", [1.0, 2.0, 3.0, 4.0, 5.0])
    entries = store.redis.xrange(store.key("sw", "tick:steps"))
    assert [json.loads(fields["record"])["wall_s"] for _, fields in entries] == [3.0, 4.0, 5.0]
    assert store.redis.lrange(store.key("sw", "tick:walls:reap"), 0, -1) == ["5.0", "4.0", "3.0", "2.0", "1.0"]


@pytest.mark.feature("swarm-tick-live-budgets")
def test_a_failed_steps_pipeline_reports_the_steps_stream(capsys: pytest.CaptureFixture[str]) -> None:
    from redis.exceptions import RedisError

    class Pipe:
        def __getattr__(self, name: str) -> Any:
            return lambda *args, **kwargs: None

        def execute(self) -> None:
            raise RedisError("down")

    class Redis:
        def pipeline(self, transaction: bool = True) -> Pipe:
            return Pipe()

    class Down:
        redis = Redis()

        def key(self, slug: str, name: str) -> str:
            return f"{slug}:{name}"

    timing.append_step(Down(), "sw", {"step": "reap", "budget_s": 1.0, "wall_s": 2.0})
    expected = {"event": "swarm_tick_record_failed", "slug": "sw", "stream": "tick:steps"}
    assert capsys.readouterr().err == json.dumps(expected) + "\n"


@pytest.mark.feature("swarm-tick-live-budgets")
def test_the_rebuild_marker_holds_one_rebuild_per_slow_interval() -> None:
    store = _store()
    marker = store.key("sw", "tick:walls-rebuilt")
    _resolved(store, "reap")
    assert store.redis.get(marker) == "1"
    assert store.redis.ttl(marker) == int(budgets.SLOW_S) == 600
    record = {"step": "reap", "budget_s": 22.0, "wall_s": 40.0, "phase": "finished"}
    for _ in range(60):
        store.redis.xadd(store.key("sw", "tick:steps"), {"record": json.dumps(record)})
    assert _resolved(store, "reap") == 22.0
    assert store.redis.exists(store.key("sw", "tick:walls:reap")) == 0


@pytest.mark.feature("swarm-tick-live-budgets")
def test_full_windows_leave_no_rebuild_marker() -> None:
    store = _store()
    for name in budgets.FLOORS:
        _push(store, name, [1.0])
    _resolved(store, "reap")
    assert store.redis.exists(store.key("sw", budgets.REBUILT)) == 0


@pytest.mark.feature("swarm-tick-live-budgets")
def test_a_rebuild_keeps_only_the_newest_window_of_budgeted_walls_of_missing_steps() -> None:
    store = _store()
    stream = store.key("sw", "tick:steps")
    for wall in [12.0] * 70:
        store.redis.xadd(stream, {"record": json.dumps({"step": "reap", "budget_s": 22.0, "wall_s": wall})})
    store.redis.xadd(stream, {"record": json.dumps({"step": "spawn", "budget_s": 33.0, "wall_s": 5.0})})
    _push(store, "spawn", [7.0])
    store.redis.xadd(stream, {"record": json.dumps({"step": "reap", "wall_s": 99.0})})
    store.redis.xadd(stream, {"record": json.dumps({"step": "reap", "budget_s": 22.0})})
    _resolved(store, "reap")
    assert store.redis.lrange(store.key("sw", "tick:walls:reap"), 0, -1) == ["12.0"] * 60
    assert store.redis.lrange(store.key("sw", "tick:walls:spawn"), 0, -1) == ["7.0"]


@pytest.mark.feature("swarm-tick-live-budgets")
def test_every_walls_pipeline_runs_without_a_transaction() -> None:
    import fakeredis

    from scripts.swarm.store import RedisStore

    flags: list[object] = []

    class Spy:
        def __init__(self, redis: Any) -> None:
            self.redis = redis

        def pipeline(self, transaction: object = True) -> Any:
            flags.append(transaction)
            return self.redis.pipeline(transaction=transaction)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.redis, name)

    store = RedisStore(Spy(fakeredis.FakeRedis(decode_responses=True)))
    record = {"step": "spawn", "budget_s": 33.0, "wall_s": 5.0}
    store.redis.xadd(store.key("sw", "tick:steps"), {"record": json.dumps(record)})
    timing.append_step(store, "sw", {"step": "reap", "budget_s": 22.0, "wall_s": 1.0})
    _resolved(store, "reap")
    assert flags == [False, False, False]


@pytest.mark.feature("swarm-tick-live-budgets")
def test_a_window_caught_between_its_push_and_trim_still_reads_sixty_walls() -> None:
    store = _store()
    store.redis.lpush(store.key("sw", "tick:walls:reap"), *["1.0"] * 61)
    assert len(budgets._walls(store, "sw")["reap"]) == budgets.WINDOW
