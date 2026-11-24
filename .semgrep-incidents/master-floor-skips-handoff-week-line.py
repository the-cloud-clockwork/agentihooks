from scripts.routing import reservation
from scripts.routing.reservation import primary_reserve_pct


def _master_floor(row, environ):
    # ruleid: master-floor-skips-handoff-week-line
    if row.week_left <= reservation.primary_reserve_pct(environ):
        return "week", row.week_left
    return None


def _take(row, environ):
    # ruleid: master-floor-skips-handoff-week-line
    if row.week_left <= primary_reserve_pct(environ):
        return True
    return False


def _master_floor_fixed(row, environ):
    # ok: master-floor-skips-handoff-week-line
    if row.week_left <= reservation.master_week_floor(environ):
        return "week", row.week_left
    return None
