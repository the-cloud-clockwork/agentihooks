"""Interval enforcement of the tick engine: wide and slow lane steps skip passes inside their interval."""

import json
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
    effects: frozenset[str] = frozenset()
    feature: str = ""
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
    assert [
        (row["skipped"], row["rested"], row["abandoned"], row["wall_s"], row["budget_s"], row["start_s"])
        for row in skipped
    ] == [(True, True, False, 0.0, 5.0, 5.0)] * 2


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
def test_a_skipped_step_in_a_parallel_pass_ends_for_its_dependents(recording: Any) -> None:
    from scripts.tick import pool

    _, _, clock = recording
    wide = Counted("wide", budgets.WIDE_S)
    after = Counted("after", budgets.CRITICAL_S, needs=frozenset({"wide"}))
    with pool.running(workers=2):
        outcomes = passes((wide, after), clock, 2)
    assert outcomes == [[engine.OK, engine.OK], [engine.SKIPPED, engine.OK]]
    assert (len(wide.runs), len(after.runs)) == (1, 2)


@pytest.mark.feature("swarm-tick-step-isolation")
def test_a_handed_off_slow_lane_step_in_a_parallel_pass_rests_inside_its_interval(recording: Any) -> None:
    from scripts.tick import pool

    _, _, clock = recording
    slow = Counted("slow", budgets.WIDE_S, lane=engine.SLOW)
    handed: list[list[str]] = []
    for index in range(2):
        start = BASE_S + index * 60.0
        clock[0] = start + 5.0
        token = timing.PASS.set({"pass_id": f"p{index}", "start_ms": start * 1000})
        try:
            with pool.running(workers=2):
                engine.run_group(
                    "place",
                    Context(View(), Deadline.after(60.0)),
                    (slow,),
                    finished=lambda records: handed.append([record.outcome for record in records]),
                )
        finally:
            timing.PASS.reset(token)
    assert handed == [[engine.OK], [engine.SKIPPED]]
    assert len(slow.runs) == 1


@pytest.mark.feature("swarm-tick-step-isolation")
def test_a_failed_run_records_its_end_and_a_late_run_does_not(recording: Any) -> None:
    store, _, _ = recording
    token = timing.PASS.set({"pass_id": "p0", "start_ms": BASE_S * 1000})
    try:
        for record, kept in (
            (engine.StepRecord("failed", 1.0, 5.0, "builtins.ValueError"), True),
            (engine.StepRecord("late", 9.0, 5.0), False),
            (engine.StepRecord("held", 0.0, 5.0, held=True), False),
        ):
            engine._journal(Counted(record.name, budgets.WIDE_S), record, 0.0)
            assert (record.name in store.redis.hgetall(store.key("sw", timing.STEP_RAN))) is kept
    finally:
        timing.PASS.reset(token)


@pytest.mark.feature("swarm-tick-step-isolation")
def test_nothing_rests_without_a_recording_store() -> None:
    assert engine.resting((Counted("wide", budgets.WIDE_S),), 60.0) == frozenset()


@pytest.mark.feature("swarm-tick-step-isolation")
def test_a_step_never_recorded_is_due(recording: Any) -> None:
    assert engine.resting((Counted("wide", budgets.WIDE_S),), 60.0) == frozenset()


@pytest.mark.feature("swarm-tick-step-isolation")
def test_nothing_rests_with_a_swarm_but_no_store() -> None:
    with timing.tick("sw", None):
        assert engine.resting((Counted("wide", budgets.WIDE_S),), 60.0) == frozenset()


@pytest.mark.feature("swarm-tick-step-isolation")
def test_a_pass_of_critical_steps_reads_no_step_ends(recording: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_store: object, _slug: str) -> dict[str, float]:
        raise AssertionError("read step ends")

    monkeypatch.setattr(timing, "step_ends", refuse)
    assert engine.resting((Counted("critical", budgets.CRITICAL_S),), 60.0) == frozenset()


@pytest.mark.feature("swarm-tick-step-isolation")
def test_a_step_at_one_cron_minute_never_rests(recording: Any) -> None:
    store, _, clock = recording
    store.redis.hset(store.key("sw", timing.STEP_RAN), "minute", repr(clock[0]))
    assert engine.resting((Counted("minute", engine.CRON_S),), 60.0) == frozenset()
    store.redis.hset(store.key("sw", timing.STEP_RAN), "wide", repr(clock[0]))
    assert engine.resting((Counted("wide", budgets.WIDE_S),), 60.0) == frozenset({"wide"})


@pytest.mark.feature("swarm-tick-step-isolation")
def test_a_step_end_storage_failure_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    from unittest.mock import patch

    from redis.exceptions import ConnectionError

    store = Store()
    with patch.object(store.redis, "hset", side_effect=ConnectionError("private connection details")):
        timing.step_ended(store, "sw", "wide", BASE_S)
    assert (
        capsys.readouterr().err
        == json.dumps({"event": "swarm_tick_record_failed", "slug": "sw", "stream": "tick:step-ran"}) + "\n"
    )


@pytest.mark.feature("swarm-tick-step-isolation")
def test_a_step_above_sixty_seconds_writing_a_pass_box_is_refused() -> None:
    step = Counted("boxed", budgets.WIDE_S, writes=frozenset({Domain("pass:spawn")}))
    with pytest.raises(ValueError, match=r"^Interval of boxed above 60s writes pass:spawn$"):
        registry._validate_timing([step])
    both = Counted("both", budgets.WIDE_S, writes=frozenset({Domain("pass:spawn"), Domain("pass:agents")}))
    with pytest.raises(ValueError, match=r"^Interval of both above 60s writes pass:agents, pass:spawn$"):
        registry._validate_timing([both])
    registry._validate_timing([Counted("boxed", budgets.CRITICAL_S, writes=frozenset({Domain("pass:spawn")}))])
    registry._validate_timing([Counted("plain", budgets.WIDE_S, writes=frozenset({Domain("passes")}))])


@pytest.mark.feature("swarm-tick-step-isolation")
def test_every_registered_step_writing_the_pass_state_runs_every_pass() -> None:
    wide = sorted(
        step.name
        for step in registry.steps()
        if any(domain == PASS or domain.startswith(registry.PASS_BOX) for domain in step.writes)
        and step.interval_s > registry.PASS_BOX_INTERVAL_S
    )
    assert wide == []
