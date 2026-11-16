"""Review rounds and reader launches per swarm task, kept on the ledger task so every hive reads the same review: the
third round on a counter closes review and a fourth is refused."""

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Protocol

from scripts.gates.log import gates_dir
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


def counts(row: dict[str, Any]) -> dict[str, int]:
    """The rounds a ledger task row records on each counter."""
    return dict(row.get("review_rounds") or {})


def readers(row: dict[str, Any]) -> list[str]:
    """The reader sub-agents a ledger task row records as launched."""
    return list(row.get("review_readers") or [])


def start(slug: str, task: str, ledger: Ledger, by: str, home: Path | None = None) -> dict[str, str | int]:
    """Spend one review round of the task on its ledger row, refusing a fourth on the same counter."""
    row = ledger.task(slug, task) or {}
    name, held = counter(row.get("pr_url") or ""), counts(row)
    spent = held.get(name, 0) + 1
    if spent > ROUNDS:
        raise SwarmError(f"task {task} gets no fourth review round. {CLOSED}")
    ledger.update_task(slug, task, {"review_rounds": {**held, name: spent}}, by=by)
    log = gates_dir(slug, home) / "reviews" / "rounds.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as stream:
        stream.write(json.dumps({"task": task, "counter": name, "round": spent, "at": int(time.time() * 1000)}) + "\n")
    return {"task": task, "round": spent, **({"notice": CLOSED} if spent == ROUNDS else {})}


def record_reader(slug: str, task: str, reader: str, ledger: Ledger, by: str) -> None:
    """Add the reader to the task's review readers on the ledger, guarded on the readers it read, and write again while
    a racing writer changes them first, at most five writes."""
    for _ in range(WRITES):
        held = readers(ledger.task(slug, task) or {})
        if reader in held:
            return
        ledger.update_task(
            slug, task, {"review_readers": sorted({*held, reader})}, by=by, if_match={"review_readers": held}
        )
