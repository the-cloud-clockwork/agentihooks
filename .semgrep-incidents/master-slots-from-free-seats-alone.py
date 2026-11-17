from typing import Any

from scripts.swarm import capacity


def _master_selection_seated(rows: list[Any]) -> dict[str, Any]:
    # ruleid: master-slots-from-free-seats-alone
    accounts = {row.name: row for row in rows if row.harness == "claude" and capacity.free_seats(row)}
    return accounts


def _master_selection_forced(rows: list[Any], task: dict[str, Any]) -> dict[str, Any]:
    # ok: master-slots-from-free-seats-alone
    accounts = {row.name: row for row in rows if row.harness == "claude" and capacity.free_seats(row)}
    if not accounts and task.get("forced"):
        accounts = {row.name: row for row in rows if row.harness == "claude"}
    return accounts


def _worker_selection(rows: list[Any]) -> dict[str, Any]:
    # ok: master-slots-from-free-seats-alone
    accounts = {row.name: row for row in rows if capacity.free_seats(row)}
    return accounts
