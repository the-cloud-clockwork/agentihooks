from collections.abc import Iterable, Mapping

from scripts.routing.slots import Slot

FRESH_SECONDS = 15 * 60
WEEK_FLOOR = 5.0
FIVE_HOUR_BANDS = ((60.0, 6), (40.0, 4), (10.0, 3), (5.0, 2))
TOP_BAND = FIVE_HOUR_BANDS[0][1]
HANDOFF_FIVE = 5.0
HIVE_CEILING = 12
HIVE_CEILING_ENV = "AGENTIHOOKS_HIVE_SESSION_CEILING"


Seat = Slot


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


def scaled_cap(five_left: float | None, week_left: float | None, ceiling: int) -> int | None:
    banded = cap(five_left, week_left)
    return banded and min(ceiling, max(banded, int(ceiling * min(five_left, week_left) / 100)))


def hive_ceiling(environ: Mapping[str, str]) -> int:
    try:
        return int(environ.get(HIVE_CEILING_ENV) or HIVE_CEILING)
    except ValueError:
        return HIVE_CEILING


def upcoming(resets_at: float | None, now: float) -> float | None:
    return resets_at if resets_at is not None and resets_at > now else None


def spend_by(five_left: float | None, resets_at: float | None) -> float | None:
    return resets_at if five_left is None or five_left > HANDOFF_FIVE else None


def _order(seat: Seat) -> tuple:
    return (seat.sessions, seat.spend_before is None, seat.spend_before, seat.harness, seat.account)


def pick(seats: Iterable[Seat]) -> Seat | None:
    return min((seat for seat in seats if seat.free), key=_order, default=None)
