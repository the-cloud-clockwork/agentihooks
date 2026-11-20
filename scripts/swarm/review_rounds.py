"""Review rounds and reader launches per swarm task, kept on the ledger task so every hive reads the same review: the
third round on a counter closes review and a fourth is refused."""

import hashlib
import time
import uuid
from typing import Any, Protocol

from scripts.swarm.store import SwarmError

ROUNDS = 3
WRITES = 5
CLOSED = (
    "Review is closed after three rounds: any finding still open gets one ruling from the other reader, "
    "then the merge proceeds."
)


class Ledger(Protocol):
    """The ledger reads and writes review state needs: one task row and a guarded update of its fields."""

    def task(self, slug: str, task_id: str) -> dict[str, Any] | None: ...

    def update_task(
        self, slug: str, task_id: str, fields: dict[str, Any], *, by: str, if_match: dict[str, Any] | None = None
    ) -> object: ...


def counter(pr_url: str) -> str:
    """The round counter a review spends: one before the task records a pull request, then one per pull request."""
    return f"rounds-{hashlib.sha256(pr_url.encode()).hexdigest()}" if pr_url else "rounds-before-pr"


def rounds(row: dict[str, Any]) -> dict[str, list[str]]:
    """The stamps a ledger task row records on each round counter, one stamp per round spent."""
    return {name: list(stamps) for name, stamps in (row.get("review_rounds") or {}).items()}


def counts(row: dict[str, Any]) -> dict[str, int]:
    """The rounds a ledger task row records on each counter."""
    return {name: len(stamps) for name, stamps in rounds(row).items()}


def stamped_at(stamp: str) -> int | None:
    """The wall clock milliseconds a round stamp opens with, or None for a stamp that carries no time."""
    clock, dash, _ = stamp.partition("-")
    return int(clock) if dash and clock.isascii() and clock.isdigit() else None


def readers(row: dict[str, Any]) -> list[str]:
    """The reader sub-agents a ledger task row records as launched."""
    return list(row.get("review_readers") or [])


def held_row(slug: str, task: str, ledger: Ledger) -> dict[str, Any]:
    """The task's ledger row, refusing a task the ledger does not hold before any review write is spent on it."""
    row = ledger.task(slug, task)
    if not row:
        raise SwarmError(f"task {task} is not on ledger {slug}")
    return row


def start(slug: str, task: str, ledger: Ledger, by: str) -> dict[str, str | int]:
    """Spend one review round of the task on its ledger row, refusing a fourth on the same counter. The round adds a
    stamp of its own, its wall clock milliseconds ahead of a unique part, under a guard on the rounds it read, so a
    round racing it lands first and this one counts after it, at most five writes."""
    stamp, row = f"{int(time.time() * 1000)}-{uuid.uuid4().hex}", held_row(slug, task, ledger)
    for _ in range(WRITES):
        name, held = counter(row.get("pr_url") or ""), rounds(row)
        spent = len(held.get(name, [])) + 1
        if spent > ROUNDS:
            raise SwarmError(f"task {task} gets no fourth review round. {CLOSED}")
        fields = {"review_rounds": {**held, name: [*held.get(name, []), stamp]}}
        ledger.update_task(slug, task, fields, by=by, if_match={"review_rounds": held})
        row = ledger.task(slug, task) or {}
        if stamp in rounds(row).get(name, []):
            return {"task": task, "round": spent, **({"notice": CLOSED} if spent == ROUNDS else {})}
    raise SwarmError(f"task {task} spent no review round: its review rounds changed under each of {WRITES} writes")


def record_reader(slug: str, task: str, reader: str, ledger: Ledger, by: str) -> None:
    """Add the reader to the task's review readers on the ledger, guarded on the readers it read, and write again while
    a racing writer changes them first, at most five writes."""
    held = readers(held_row(slug, task, ledger))
    for _ in range(WRITES):
        if reader in held:
            return
        ledger.update_task(
            slug, task, {"review_readers": sorted({*held, reader})}, by=by, if_match={"review_readers": held}
        )
        held = readers(ledger.task(slug, task) or {})
    if reader not in held:
        raise SwarmError(
            f"reader {reader} not recorded on task {task}: its review readers changed under {WRITES} writes"
        )
