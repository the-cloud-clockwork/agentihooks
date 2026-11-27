from typing import Any

CLOSED = "CLOSED"
FLOOR = 2.0


def _master_closed(row: Any) -> bool:
    # ok: master-closed-at-worker-drain-floor
    return row.state == CLOSED and row.week_left <= FLOOR


def _master_closed_bare(row: Any) -> bool:
    # ruleid: master-closed-at-worker-drain-floor
    return row.state == CLOSED


def _master_closed_quoted(row: Any) -> bool:
    # ruleid: master-closed-at-worker-drain-floor
    return row.state == "CLOSED"


def _master_selection_drained(rows: list[Any]) -> frozenset[str]:
    # ruleid: master-closed-at-worker-drain-floor
    return frozenset(row.name for row in rows if row.harness == "claude" and row.state == CLOSED)


def _master_selection_quoted(rows: list[Any]) -> frozenset[str]:
    # ruleid: master-closed-at-worker-drain-floor
    return frozenset(row.name for row in rows if row.state == "CLOSED")


def _master_selection_floored(rows: list[Any]) -> frozenset[str]:
    # ok: master-closed-at-worker-drain-floor
    return frozenset(row.name for row in rows if row.harness == "claude" and _master_closed(row))
