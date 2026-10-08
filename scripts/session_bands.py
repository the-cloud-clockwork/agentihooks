from collections.abc import Iterable
from dataclasses import dataclass

FRESH_SECONDS = 15 * 60
WEEK_FLOOR = 5.0
FIVE_HOUR_BANDS = ((60.0, 6), (40.0, 4), (10.0, 3), (5.0, 2))
TOP_BAND = FIVE_HOUR_BANDS[0][1]
HANDOFF_FIVE = 5.0
HANDOFF_WEEK = 2.0


@dataclass(frozen=True)
class Seat:
    harness: str
    account: str
    cap: int
    sessions: int
    week_resets_at: float | None = None

    @property
    def free(self) -> int:
        return max(0, self.cap - self.sessions)


def left(used: float | None, resets_at: int | None, now: float) -> float | None:
    if resets_at is not None and resets_at <= now:
        return 100.0
    return None if used is None else max(0.0, 100.0 - used)


def fresh(observed_at: float | None, now: float) -> bool:
    return observed_at is not None and now - observed_at <= FRESH_SECONDS


def five_hour_cap(five_left: float) -> int:
    return next((seats for floor, seats in FIVE_HOUR_BANDS if five_left >= floor), 0)


def week_cap(week_left: float | None) -> int | None:
    if week_left is None:
        return None
    return 0 if week_left < WEEK_FLOOR else TOP_BAND


def cap(five_left: float | None, week_left: float | None) -> int | None:
    if five_left is None:
        return None
    week = week_cap(week_left)
    return None if week is None else min(week, five_hour_cap(five_left))


def spend_by(five_left: float | None, week_left: float | None, resets_at: float | None) -> float | None:
    above = week_left is not None and week_left > HANDOFF_WEEK and (five_left is None or five_left > HANDOFF_FIVE)
    return resets_at if above else None


def _order(seat: Seat) -> tuple:
    reset = seat.week_resets_at
    return (seat.sessions, reset is None, reset or 0, seat.harness, seat.account)


def pick(seats: Iterable[Seat]) -> Seat | None:
    return min((seat for seat in seats if seat.free), key=_order, default=None)
