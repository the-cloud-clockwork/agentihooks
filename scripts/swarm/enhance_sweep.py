"""The enhance sweep: run the thin task check over every open engineer and ci task read through the ledger API and
queue an enhancement plan task for each thin one through the thin task pass writer, pausing between writes."""

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
OUTCOMES = ("queued", "failed", "skipped")


def _thin(tasks: list[dict[str, Any]]) -> list[tuple[dict[str, Any], list[str]]]:
    """Return each task the thin task pass would queue with the rules it fails, in ledger order."""
    checked = ((task, thin_task.thin({"description": "", **task})) for task in filter(thin.unchecked, tasks))
    return [(task, problems) for task, problems in checked if problems]


def thin_by_lane(tasks: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Return the ids of the thin tasks the thin task pass would queue, keyed by lane."""
    lanes: dict[str, list[str]] = {lane: [] for lane in thin.LANES}
    for task, _ in _thin(tasks):
        lanes[task.get("lane", "eng")].append(task["id"])
    return lanes


def _say(line: str) -> None:
    """Print one action line and flush it, so it shows before the next thin task's write starts."""
    print(line, flush=True)


def _enhance(ledger: Ledger, task: dict[str, Any], problems: list[str], ids: set[str]) -> tuple[str, str]:
    """Write one thin task's enhancement under its own deadline; return its action line and outcome, where a write
    that times out has failed and a write the ledger refused is skipped."""
    try:
        line, landed = thin.enhance(ledger, Deadline.after(BUDGET), task, problems, ids)
    except TimeoutError as exc:
        return f"failed the enhancement of {task['id']}: {exc}", "failed"
    return line, "queued" if landed else "skipped"


def sweep(
    tasks: list[dict[str, Any]],
    ledger: Ledger,
    *,
    pause: Callable[[float], None],
    report: Callable[[str], None] = _say,
) -> dict[str, int]:
    """Queue enhancement for every thin task through the thin task pass writer, reporting each task's action line as
    its write ends; return how many thin tasks were queued, failed and skipped."""
    ids = {task["id"] for task in tasks}
    counts = dict.fromkeys(OUTCOMES, 0)
    for index, (task, problems) in enumerate(_thin(tasks)):
        if index:
            pause(PAUSE)
        line, outcome = _enhance(ledger, task, problems, ids)
        report(line)
        counts[outcome] += 1
    return counts


def command(store: Any, args: argparse.Namespace) -> None:
    """Print the thin tasks per lane of swarm ``args.slug`` and, unless a dry run, queue their enhancement, printing
    each task's line as its write ends and then the counts."""
    tasks = ledger_client.LedgerClient().tasks(args.slug)
    lanes = {lane: {"count": len(ids), "ids": ids} for lane, ids in thin_by_lane(tasks).items()}
    ledger = None if args.dry_run else ClientLedger(args.slug, resource_client(args.slug))
    counts = dict.fromkeys(OUTCOMES, 0) if ledger is None else sweep(tasks, ledger, pause=time.sleep)
    print(json.dumps({"dry_run": args.dry_run, "lanes": lanes, "counts": counts}))
