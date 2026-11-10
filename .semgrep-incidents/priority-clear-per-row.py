from typing import Any


def sweep_each(ledger: Any, slug: str, stale: list[tuple[dict[str, Any], str]]) -> None:
    for row, reason in stale:
        # ruleid: priority-clear-per-row
        ledger.clear_priority(slug, row["id"], reason)


def sweep_until_empty(ledger: Any, slug: str, stale: list[tuple[dict[str, Any], str]]) -> None:
    while stale:
        row, reason = stale.pop()
        # ruleid: priority-clear-per-row
        ledger.clear_priority(slug, row["id"], reason)


def sweep_listed(ledger: Any, slug: str, stale: list[tuple[dict[str, Any], str]]) -> list[None]:
    # ruleid: priority-clear-per-row
    return [ledger.clear_priority(slug, row["id"], reason) for row, reason in stale]


def sweep_batched(ledger: Any, slug: str, stale: list[tuple[dict[str, Any], str]]) -> None:
    # ok: priority-clear-per-row
    ledger.clear_priorities(slug, [(row["id"], reason) for row, reason in stale])


def resolve_one(ledger: Any, slug: str, row: dict[str, Any], reason: str) -> None:
    # ok: priority-clear-per-row
    ledger.clear_priority(slug, row["id"], reason)
