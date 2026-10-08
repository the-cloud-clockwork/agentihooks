from collections.abc import Sequence
from typing import Protocol

WEEK_HOURS = 168.0
FULL_PACE = 100.0 / WEEK_HOURS
MIN_HOURS = 1.0
MIN_LEFT = 5.0
FIVE_HOUR_STATES = ((90.0, "DRAIN"), (80.0, "DRAIN_SOON"), (65.0, "REDUCE"))
PACE_STATES = ((0.5, "NORMAL"), (0.25, "REDUCE"))
SEVERITY = ("NORMAL", "REDUCE", "DRAIN_SOON", "DRAIN")


class Window(Protocol):
    used: float | None
    resets_at: int | None

    @property
    def remaining(self) -> float | None: ...


def hours_left(window: Window, now: float) -> float:
    if window.resets_at is None:
        return WEEK_HOURS
    return max(MIN_HOURS, (window.resets_at - now) / 3600)


def _week_rate(window: Window, now: float) -> float:
    return window.remaining / hours_left(window, now)


def rate(five: Window, weeks: Sequence[Window], now: float) -> float | None:
    if five.remaining is None or any(week.remaining is None for week in weeks):
        return None
    if five.remaining < MIN_LEFT:
        return 0.0
    return min(_week_rate(week, now) for week in weeks)


def state(five: Window, weeks: Sequence[Window], now: float) -> str:
    spendable = rate(five, weeks, now)
    if spendable is None:
        return "UNKNOWN"
    five_state = next((name for used, name in FIVE_HOUR_STATES if five.used >= used), "NORMAL")
    week_state = next((name for pace, name in PACE_STATES if spendable >= pace * FULL_PACE), "DRAIN")
    return max(five_state, week_state, key=SEVERITY.index)


def routable(five: Window, weeks: Sequence[Window], now: float) -> bool:
    spendable = rate(five, weeks, now)
    return bool(spendable) and all(week.remaining >= MIN_LEFT or _week_rate(week, now) >= FULL_PACE for week in weeks)
