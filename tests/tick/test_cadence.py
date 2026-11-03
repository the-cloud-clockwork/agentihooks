from dataclasses import dataclass

import pytest

from scripts.swarm import scheduled_tick
from scripts.tick import engine
from scripts.tick.step import Lane


@dataclass(frozen=True)
class TimedStep:
    lane: Lane
    interval_s: float


@pytest.mark.parametrize("minutes", [1, 2, 3, 4, 5])
def test_every_guard_outlasts_one_missed_slow_pass_at_one_to_five_minutes(minutes: int) -> None:
    assert engine.cadence_breaches(minutes * 60.0) == []
    assert engine.cadence_s({engine.CADENCE_ENV: str(minutes)}) == minutes * 60.0


def test_six_minutes_breaks_the_delivery_guard_and_heartbeat_limit() -> None:
    assert engine.cadence_breaches(360.0) == ["delivery guard", "heartbeat limit"]
    with pytest.raises(ValueError, match="delivery guard, heartbeat limit"):
        engine.cadence_s({engine.CADENCE_ENV: "6"})


@pytest.mark.parametrize("raw", ["0", "-1", "two", "1.5"])
def test_a_cadence_outside_whole_positive_minutes_is_refused(raw: str) -> None:
    with pytest.raises(ValueError, match=engine.CADENCE_ENV):
        engine.cadence_s({engine.CADENCE_ENV: raw})


def test_an_unset_cadence_is_one_minute() -> None:
    assert engine.cadence_s({}) == 60.0
    assert engine.cadence_s({engine.CADENCE_ENV: " "}) == 60.0


@pytest.mark.parametrize("minutes", [1, 2, 3, 4, 5])
def test_the_cron_stays_every_minute(minutes: int) -> None:
    assert engine.cadence_s({engine.CADENCE_ENV: str(minutes)}) == minutes * engine.CRON_S
    assert scheduled_tick.EXPRESSION == "* * * * *"


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        (TimedStep(engine.SLOW, 60.0), 180.0),
        (TimedStep(engine.SLOW, 600.0), 600.0),
        (TimedStep(Lane("critical"), 60.0), 60.0),
    ],
)
def test_a_slow_lane_interval_stretches_to_the_cadence(step: TimedStep, expected: float) -> None:
    assert engine.interval_s(step, 180.0) == expected


@pytest.fixture
def guards(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    from scripts.swarm import lease, tick_heartbeat, tick_watch

    monkeypatch.setattr(scheduled_tick, "FRESH_S", 600.0)
    monkeypatch.setattr(tick_heartbeat, "LIMIT_MS", 600_000)
    monkeypatch.setattr(tick_watch, "PASS_S", 60)
    monkeypatch.setattr(tick_watch, "SLUG_PASS_S", 120)
    monkeypatch.setattr(lease, "ttl_ms", lambda: 120_000)
    return monkeypatch


def test_a_delivery_guard_of_exactly_two_cadences_holds(guards: pytest.MonkeyPatch) -> None:
    assert "delivery guard" not in engine.cadence_breaches(300.0)
    assert "delivery guard" in engine.cadence_breaches(301.0)


def test_a_lease_ttl_of_exactly_two_cron_minutes_holds(guards: pytest.MonkeyPatch) -> None:
    from scripts.swarm import lease

    assert "lease TTL" not in engine.cadence_breaches(60.0)
    guards.setattr(lease, "ttl_ms", lambda: 119_000)
    assert engine.cadence_breaches(60.0) == ["lease TTL"]


def test_a_stale_watcher_window_of_exactly_the_heartbeat_limit_less_the_cadence_holds(
    guards: pytest.MonkeyPatch,
) -> None:
    assert "stale watcher" not in engine.cadence_breaches(420.0)
    assert engine.cadence_breaches(421.0) == ["delivery guard", "heartbeat limit", "stale watcher"]
