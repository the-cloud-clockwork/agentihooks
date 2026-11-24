"""The enhance sweep: run the thin task check over every open engineer and ci task read through the ledger API that no
freeze holds and queue an enhancement plan task for each thin one through the thin task pass writer, pausing between
writes."""

import argparse
import json
import time
from collections.abc import Callable
from typing import Any

from scripts.swarm import ledger_client, thin_task
from scripts.tick.adapters.ledger import ClientLedger, resource_client
from scripts.tick.deadline import Deadline
from scripts.tick.ports.contracts import Ledger
from scripts.tick.steps import thin

BUDGET = 30.0
PAUSE = 3.0
Unfrozen = Callable[[dict[str, Any]], bool]


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
    tasks: list[dict[str, Any]], unfrozen: Unfrozen, ledger: Ledger, *, pause: Callable[[float], None]
) -> list[str]:
    """Queue enhancement for every thin task through the thin task pass writer; return its action lines."""
    ids = {task["id"] for task in tasks}
    lines: list[str] = []
    for index, (task, problems) in enumerate(_thin(tasks, unfrozen)):
        if index:
            pause(PAUSE)
        lines.append(thin.enhance(ledger, Deadline.after(BUDGET), task, problems, ids)[0])
    return lines


def command(store: Any, args: argparse.Namespace) -> None:
    """Print the thin tasks per lane of swarm ``args.slug`` that no freeze holds and, unless a dry run, queue their
    enhancement."""
    client = ledger_client.LedgerClient()
    tasks = client.tasks(args.slug)
    unfrozen = thin.unfrozen({"tasks": tasks, "freezes": client.freezes(args.slug), **client.work_tree(args.slug)})
    lanes = {lane: {"count": len(ids), "ids": ids} for lane, ids in thin_by_lane(tasks, unfrozen).items()}
    ledger = None if args.dry_run else ClientLedger(args.slug, resource_client(args.slug))
    actions = [] if ledger is None else sweep(tasks, unfrozen, ledger, pause=time.sleep)
    print(json.dumps({"dry_run": args.dry_run, "lanes": lanes, "actions": actions}))
