from typing import Any, Protocol


class Lineage(Protocol):
    id: str


def stale(rows: list[dict[str, Any]], lineage: Lineage) -> bool:
    # ruleid: incident-expert-stale-pending-transfer
    return any(row["task"] == lineage.id and not row["successor"] for row in rows)


def current(latest: dict[str, Any] | None) -> bool:
    # ok: incident-expert-stale-pending-transfer
    return latest is not None and not latest["successor"]
