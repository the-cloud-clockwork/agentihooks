"""The enhance sweep: run the thin task check over every open engineer and ci task read through the ledger API that no
freeze holds and queue an enhancement plan task for each thin one through the thin task pass writer, then release each
task held for enhancement whose thin task check now passes, pausing between writes."""

import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from scripts.swarm import ledger_client, thin_task
from scripts.tick.adapters.ledger import ClientLedger, resource_client
from scripts.tick.adapters.writer_client import principal
from scripts.tick.deadline import Deadline
from scripts.tick.ports.contracts import Ledger
from scripts.tick.steps import thin

BUDGET = 30.0
PAUSE = 3.0
NEEDED = "needed"
SKIPPED = "skipped"
RELEASED = "released thin task {}: its spec states its proof"
Unfrozen = Callable[[dict[str, Any]], bool]


@dataclass(frozen=True)
class SweepResult:
    lines: tuple[str, ...]
    queued: int
    failed: int
    skipped: int

    @property
    def counts(self) -> dict[str, int]:
        return {"queued": self.queued, "failed": self.failed, "skipped": self.skipped}

    @property
    def wrote(self) -> bool:
        return bool(self.queued or self.failed)


@dataclass(frozen=True)
class Released:
    lines: tuple[str, ...]
    released: int
    failed: int


def _thin(tasks: list[dict[str, Any]], unfrozen: Unfrozen) -> list[tuple[dict[str, Any], list[str]]]:
    """Return each task the thin task pass would queue with the rules it fails, in ledger order, leaving out every task
    ``unfrozen`` reads as held by a freeze."""
    candidates = filter(unfrozen, filter(thin.unchecked, tasks))
    checked = ((task, thin_task.thin({"description": "", **task})) for task in candidates)
    return [(task, problems) for task, problems in checked if problems]


def thin_by_lane(tasks: list[dict[str, Any]], unfrozen: Unfrozen) -> dict[str, list[str]]:
    """Return the ids of the thin tasks the thin task pass would queue, keyed by lane."""
    lanes: dict[str, list[str]] = {lane: [] for lane in thin.LANES}
    for task, _ in _thin(tasks, unfrozen):
        lanes[task.get("lane", "eng")].append(task["id"])
    return lanes


def sweep(
    tasks: list[dict[str, Any]],
    unfrozen: Unfrozen,
    ledger: Ledger | None,
    *,
    pause: Callable[[float], None],
    emit: Callable[[str], None] | None = None,
) -> SweepResult:
    ids = {task["id"] for task in tasks}
    lines: list[str] = []
    counts = {"queued": 0, "failed": 0, "skipped": 0}
    writes = 0
    for task, problems in _thin(tasks, lambda _: True):
        if not unfrozen(task):
            line = f"skipped thin task {task['id']}: held by a freeze"
            outcome = "skipped"
        elif ledger is None:
            continue
        else:
            if writes:
                pause(PAUSE)
            writes += 1
            try:
                line, landed = thin.enhance(ledger, Deadline.after(BUDGET), task, problems, ids)
            except (OSError, ledger_client.LedgerUnreachable) as error:
                line = f"failed the enhancement of {task['id']}: {type(error).__name__} {error}"
                landed = False
            outcome = "queued" if landed else "failed"
        lines.append(line)
        counts[outcome] += 1
        if emit is not None:
            emit(line)
    return SweepResult(tuple(lines), counts["queued"], counts["failed"], counts["skipped"])


def held(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return each open, in scope engineer or ci task with no pull request link that the claim hold keeps for its
    enhancement mark, a missing lane reading as engineer, in ledger order."""
    return [task for task in tasks if _held(task)]


def _held(task: dict[str, Any]) -> bool:
    in_lane = task.get("lane", "eng") in thin.LANES and not task.get("pr_url")
    return _open(task) and in_lane and task.get("enhancement") == NEEDED


def releasable(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return each held task whose thin task check now passes, in ledger order."""
    return [task for task in held(tasks) if not thin_task.thin({"description": "", **task})]


def release(
    tasks: list[dict[str, Any]],
    ledger: Ledger | None,
    by: str,
    *,
    pause: Callable[[float], None],
    emit: Callable[[str], None] | None = None,
    wrote: bool = False,
) -> Released:
    """Mark the enhancement of each releasable task skipped and scope out its open enhancement plan task, one ledger
    write by ``by`` per task, pausing before each write after the first, or before every write when an earlier pass
    ``wrote``; with no ``ledger``, a dry run, nothing is written."""
    if ledger is None:
        return Released((), 0, 0)
    plans = {task["id"] for task in tasks if task.get("lane") == "plan" and _open(task)}
    lines: list[str] = []
    released = 0
    for index, task in enumerate(releasable(tasks)):
        if index or wrote:
            pause(PAUSE)
        line, landed = _release(ledger, task["id"], _releasing(task["id"], plans, by))
        lines.append(line)
        released += landed
        if emit is not None:
            emit(line)
    return Released(tuple(lines), released, len(lines) - released)


def _open(task: dict[str, Any]) -> bool:
    return task.get("state") == "open" and not task.get("out_of_scope")


def _releasing(tid: str, plans: set[str], by: str) -> list[dict[str, object]]:
    """Return the guarded skip of the enhancement of task ``tid`` and the scope out of its plan task when ``plans``
    holds it open, both by ``by``."""
    plan = thin_task.ENHANCE.format(tid)
    mark: dict[str, object] = {
        "op": "task_enhancement",
        "id": f"thin-release-{tid}",
        "by": by,
        "item": f"tasks/{tid}",
        "expected": NEEDED,
        "enhancement": SKIPPED,
    }
    scope: dict[str, object] = {
        "op": "set",
        "id": f"thin-scope-{tid}",
        "by": by,
        "path": f"tasks/{plan}/out_of_scope",
        "value": True,
    }
    return [mark, scope] if plan in plans else [mark]


def _release(ledger: Ledger, tid: str, operations: list[dict[str, object]]) -> tuple[str, bool]:
    """Write the release ``operations`` of task ``tid`` under its own deadline; return the action line and whether
    the write landed."""
    try:
        ledger.mutate(operations, deadline=Deadline.after(BUDGET))
    except ledger_client.LedgerRefused as refusal:
        return f"refused the release of {tid}: {refusal}", False
    except (OSError, ledger_client.LedgerUnreachable) as error:
        return f"failed the release of {tid}: {type(error).__name__} {error}", False
    return RELEASED.format(tid), True


def _emit(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def command(store: Any, args: argparse.Namespace) -> None:
    client = ledger_client.LedgerClient()
    tasks = client.tasks(args.slug)
    unfrozen = thin.unfrozen({"tasks": tasks, "freezes": client.freezes(args.slug), **client.work_tree(args.slug)})
    lanes = {lane: {"count": len(ids), "ids": ids} for lane, ids in thin_by_lane(tasks, unfrozen).items()}
    candidates = [task["id"] for task in releasable(tasks)]
    ledger = None if args.dry_run else ClientLedger(args.slug, resource_client(args.slug))
    result = sweep(tasks, unfrozen, ledger, pause=time.sleep, emit=_emit)
    by = principal() or thin.SENDER
    freed = release(tasks, ledger, by, pause=time.sleep, emit=_emit, wrote=result.wrote)
    report = {"count": len(candidates), "ids": candidates, "released": freed.released, "failed": freed.failed}
    print(
        json.dumps(
            {
                "dry_run": args.dry_run,
                "held": len(held(tasks)),
                "lanes": lanes,
                "release": report,
                "actions": [*result.lines, *freed.lines],
                "counts": result.counts,
            }
        )
    )
