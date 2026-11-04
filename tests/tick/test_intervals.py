"""Interval enforcement of the tick engine: wide and slow lane steps skip passes inside their interval."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

import scripts.swarm.cli  # noqa: F401
from scripts.swarm import timing
from scripts.tick import engine, registry
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result
from scripts.tick.steps import PASS, budgets

pytestmark = pytest.mark.unit

BASE_S = 1_700_000_000.0


class View:
    def read(self, domain: Domain, key: str) -> object | None:
        return None


class Store:
    def __init__(self) -> None:
        import fakeredis

        self.redis = fakeredis.FakeRedis(decode_responses=True)

    def key(self, slug: str, name: str) -> str:
        return f"swarm:{slug}:{name}"


@dataclass
class Counted:
    name: str
    interval_s: float
    lane: Lane = Lane("tick")
    needs: frozenset[str] = frozenset()
    group: str = "place"
    budget_s: float = 5.0
    reads: frozenset[Domain] = frozenset()
    writes: frozenset[Domain] = frozenset()
    runs: list[float] = field(default_factory=list)

    def run(self, ctx: Context) -> Result:
        self.runs.append(1.0)
        return Result()


@pytest.fixture
def recording(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Store, list[dict[str, Any]], list[float]]]:
    store = Store()
    rows: list[dict[str, Any]] = []
    clock = [BASE_S]
    monkeypatch.setattr(timing, "append_record", lambda _store, _slug, _stream, row: rows.append(row))
    monkeypatch.setattr(engine.time, "time", lambda: clock[0])
    monkeypatch.delenv(engine.CADENCE_ENV, raising=False)
    with timing.tick("sw", store):
        yield store, rows, clock


def passes(steps: tuple[Counted, ...], clock: list[float], count: int, gap_s: float = 60.0) -> list[list[str]]:
    outcomes: list[list[str]] = []
    for index in range(count):
        start = BASE_S + index * gap_s
        clock[0] = start + 5.0
        token = timing.PASS.set({"pass_id": f"p{index}", "start_ms": start * 1000})
        try:
            records = engine.run_group("place", Context(View(), Deadline.after(60.0)), steps)
        finally:
            timing.PASS.reset(token)
        outcomes.append([record.outcome for record in records])
    return outcomes


@pytest.mark.feature("swarm-tick-step-isolation")
def test_a_wide_step_runs_every_other_pass_and_a_critical_step_every_pass(recording: Any) -> None:
    store, rows, clock = recording
    wide, critical = Counted("wide", budgets.WIDE_S), Counted("critical", budgets.CRITICAL_S)
    outcomes = passes((wide, critical), clock, 4)
    assert outcomes == [
        [engine.OK, engine.OK],
        [engine.SKIPPED, engine.OK],
        [engine.OK, engine.OK],
        [engine.SKIPPED, engine.OK],
    ]
    assert (len(wide.runs), len(critical.runs)) == (2, 4)
    assert store.redis.hgetall(store.key("sw", timing.STEP_RAN)) == {"wide": repr(BASE_S + 125.0)}
    skipped = [row for row in rows if row["step"] == "wide" and row["outcome"] == engine.SKIPPED]
    assert [(row["skipped"], row["rested"], row["abandoned"], row["wall_s"]) for row in skipped] == [
        (True, True, False, 0.0)
    ] * 2


@pytest.mark.feature("swarm-tick-step-isolation")
def test_a_slow_lane_step_at_cadence_three_runs_once_in_three_passes(
    recording: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, clock = recording
    monkeypatch.setenv(engine.CADENCE_ENV, "3")
    monkeypatch.setattr(engine, "cadence_breaches", lambda cadence: [])
    slow = Counted("slow", budgets.CRITICAL_S, lane=engine.SLOW)
    outcomes = passes((slow,), clock, 4)
    assert outcomes == [[engine.OK], [engine.SKIPPED], [engine.SKIPPED], [engine.OK]]


@pytest.mark.feature("swarm-tick-step-isolation")
def test_a_slow_lane_step_at_cadence_one_runs_every_pass(recording: Any) -> None:
    _, _, clock = recording
    slow = Counted("slow", budgets.CRITICAL_S, lane=engine.SLOW)
    assert passes((slow,), clock, 3) == [[engine.OK]] * 3


@pytest.mark.feature("swarm-tick-step-isolation")
def test_a_skipped_step_counts_as_ended_for_its_dependents(recording: Any) -> None:
    _, _, clock = recording
    wide = Counted("wide", budgets.WIDE_S)
    after = Counted("after", budgets.CRITICAL_S, needs=frozenset({"wide"}))
    assert passes((wide, after), clock, 2) == [[engine.OK, engine.OK], [engine.SKIPPED, engine.OK]]
    assert len(after.runs) == 2


@pytest.mark.feature("swarm-tick-step-isolation")
def test_a_failed_run_records_its_end_and_a_late_run_does_not(recording: Any) -> None:
    store, _, _ = recording
    timing.PASS.set({"pass_id": "p0", "start_ms": BASE_S * 1000})
    for record, kept in (
        (engine.StepRecord("failed", 1.0, 5.0, "builtins.ValueError"), True),
        (engine.StepRecord("late", 9.0, 5.0), False),
        (engine.StepRecord("held", 0.0, 5.0, held=True), False),
    ):
        engine._journal(Counted(record.name, budgets.WIDE_S), record, 0.0)
        assert (record.name in store.redis.hgetall(store.key("sw", timing.STEP_RAN))) is kept


@pytest.mark.feature("swarm-tick-step-isolation")
def test_nothing_rests_without_a_recording_store() -> None:
    assert engine.resting((Counted("wide", budgets.WIDE_S),), 60.0) == frozenset()


@pytest.mark.feature("swarm-tick-step-isolation")
def test_a_step_never_recorded_is_due(recording: Any) -> None:
    assert engine.resting((Counted("wide", budgets.WIDE_S),), 60.0) == frozenset()


@pytest.mark.feature("swarm-tick-step-isolation")
def test_a_step_above_sixty_seconds_writing_a_pass_box_is_refused() -> None:
    step = Counted("boxed", budgets.WIDE_S, writes=frozenset({Domain("pass:spawn")}))
    with pytest.raises(ValueError, match=r"^Interval of boxed above 60s writes pass:spawn$"):
        registry._validate_timing([step])
    registry._validate_timing([Counted("boxed", budgets.CRITICAL_S, writes=frozenset({Domain("pass:spawn")}))])
    registry._validate_timing([Counted("plain", budgets.WIDE_S, writes=frozenset({Domain("passes")}))])


@pytest.mark.feature("swarm-tick-step-isolation")
def test_every_registered_step_writing_the_pass_state_runs_every_pass() -> None:
    wide = sorted(
        step.name for step in registry.steps() if PASS in step.writes and step.interval_s > budgets.CRITICAL_S
    )
    assert wide == []
