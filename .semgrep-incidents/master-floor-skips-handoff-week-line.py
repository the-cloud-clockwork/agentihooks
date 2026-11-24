from collections.abc import Mapping
from typing import Any

from scripts.routing import reservation
from scripts.routing.reservation import primary_reserve_pct


def _master_floor(row: Any, environ: Mapping[str, str]) -> tuple[str, float] | None:
    # ruleid: master-floor-skips-handoff-week-line
    if row.week_left <= reservation.primary_reserve_pct(environ):
        return "week", row.week_left
    return None


def _take(row: Any, environ: Mapping[str, str]) -> bool:
    # ruleid: master-floor-skips-handoff-week-line
    if row.week_left <= primary_reserve_pct(environ):
        return True
    return False


def _master_floor_fixed(row: Any, environ: Mapping[str, str]) -> tuple[str, float] | None:
    # ok: master-floor-skips-handoff-week-line
    if row.week_left <= reservation.master_week_floor(environ):
        return "week", row.week_left
    return None
