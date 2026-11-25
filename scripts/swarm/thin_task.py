"""The thin task check: which rules an open task's text misses before an engineer may claim it, and the grader of the
enhancement plan section a planner writes for a thin task. Pure functions over task records and plan text."""

import argparse
import json
import re
import time
from itertools import batched
from typing import Any

from scripts.swarm.slice_check import CODE_KINDS, done_when, short
from scripts.swarm_ledger import ledger_kinds

__all__ = ["ENHANCE", "THIN_WORDS", "backfill", "exempt", "section_complete", "thin"]

BACKFILL_BUDGET = 30.0
BACKFILL_PAIRS = 25
BACKFILL_PAUSE = 1.0
BACKFILL_MAX_WRITE = 2.0

THIN_WORDS = 40
ENHANCE = "enhance-{}"
EXEMPT = ("planned", "skipped")
NAMED = re.compile(
    r"\b[A-Za-z]\w*\("
    r"|\b[a-z][a-z0-9]+(?:[._][a-z0-9]+)+"
    r"|(?<![\w/])/[\w{}-]+(?:/[\w{}-]+)*"
    r"|\b(?:agentihooks|python -m)\s+[a-z]"
    r"|\b(?:command|function|file)\s+(?:`[A-Za-z][\w./-]*`|[A-Za-z][\w./-]*)"
    r"|\bcommand\s+`[A-Za-z][\w./-]*(?:\s+[^`\n]+)?`",
    re.I,
)
OBSERVED = re.compile(
    r"\b(?:returns?|responds?|emits?|prints?|writes?|contains?|exits?|rejects?|raises?)\s+\S"
    r"|\b(?:has|have)\s+status\s+\S"
    r"|(?<=\))\s*(?:==|!=|<=|>=|<|>)\s*\S",
    re.I,
)
RECON = (
    "What will be built",
    "Code areas touched",
    "What might break",
    "Regression tests",
    "Resources needed",
    "Protection plan",
)
BODY = ("Summary", "Seams", "Tests", "Proof")


def thin(task: dict[str, Any]) -> list[str]:
    """Every rule the task fails; code and CI seams name a public boundary and observed outcome together."""
    if exempt(task):
        return []
    kind: str = ledger_kinds.kind(task)  # pyright: ignore[reportUnknownMemberType]
    tid, description = task["id"], task["description"]
    problems: list[str] = []
    if short(description, THIN_WORDS):
        problems.append(f"Task {tid} has a description under {THIN_WORDS} words.")
    if not done_when(description):
        problems.append(f"Task {tid} has no done when sentence.")
    if not task.get("territory"):
        problems.append(f"Task {tid} names no territory.")
    if kind in CODE_KINDS and not _named_seam(description):
        problems.append(f"Task {tid} is {kind} work that names no seam.")
    return problems


def _named_seam(text: str) -> bool:
    parts = re.split(r"(?<=[.!?])\s+|\n(?=\s*(?:[-*+]|\d+[.)]|#{1,6})\s)|\n\s*\n", text)
    return any(
        boundary.end() < observation.start() or observation.end() < boundary.start()
        for part in parts
        for boundary in NAMED.finditer(part)
        for observation in OBSERVED.finditer(part)
    )


def exempt(task: dict[str, Any]) -> bool:
    """Whether the task is never thin: it carries a plan slice, is plan kind work, or its enhancement is planned or
    skipped."""
    kind: str = ledger_kinds.kind(task)  # pyright: ignore[reportUnknownMemberType]
    return bool(task.get("plan_slice")) or kind == "plan" or task.get("enhancement") in EXEMPT


def section_complete(text: str) -> list[str]:
    """Every part an enhancement plan section misses: a boundary and observation under its Seams heading, each recon heading, each
    pull request body section and a done when sentence."""
    found = {name: _section(text, name) for name in (*RECON, *BODY)}
    seams = found["Seams"]
    problems = ["The section names no seam."] if seams is None or not _named_seam(seams) else []
    problems += [f"The section has no {name} heading." for name in RECON if found[name] is None]
    problems += [f"The section has no {name} pull request body section." for name in BODY if found[name] is None]
    if not done_when(text):
        problems.append("The section has no done when sentence.")
    return problems


def _section(text: str, name: str) -> str | None:
    found = re.search(rf"^#{{1,6}}[ \t]+{re.escape(name)}[ \t]*$(.*?)(?=^#|\Z)", text, re.I | re.M | re.S)
    return found.group(1) if found and found.group(1).strip() else None


def _legacy_plan(task: dict[str, Any]) -> bool:
    return (
        task.get("state") == "open"
        and task.get("lane") == "plan"
        and task.get("kind") == "plan"
        and bool(task.get("enhances"))
        and not task.get("phase")
        and not task.get("done")
    )


def backfill(tasks: list[dict[str, Any]], phases: set[str], eligible: set[str]) -> list[list[dict[str, object]]]:
    from scripts.tick.steps.thin import _move, _own_phase  # pyright: ignore[reportPrivateUsage]

    rows = {task["id"]: task for task in tasks}
    batches: list[list[dict[str, object]]] = []
    for plan in filter(_legacy_plan, tasks):
        if plan["id"] not in eligible:
            continue
        target = rows[plan["enhances"]]
        phase = target.get("phase") or plan["id"]
        operations: list[dict[str, object]] = []
        if not target.get("phase"):
            if phase not in phases:
                operations.append(_own_phase(target, phase))
            operations.append(_move(target, phase))
        operations.append(_move(plan, phase))
        batches.append(operations)
    return batches


def _claimable_plans(client: Any, slug: str) -> set[str]:
    verdict = client.focus_verdicts(slug, "lane:plan")
    if verdict is None:
        raise ValueError("Planner claim verdict is unavailable")
    return {task["id"] for task in verdict["tasks"] if task["verdict"] == "claimable"}


def _backfill_candidates(client: Any, slug: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in batched(sorted(_claimable_plans(client, slug)), 100):
        rows.extend(
            client.tasks_where(
                slug,
                ids=group,
                fields=("id", "state", "lane", "kind", "enhances", "phase", "done"),
            )
        )
    return list(filter(_legacy_plan, rows))


def _backfill_batch(
    client: Any,
    ledger: Any,
    resource: Any,
    slug: str,
    plans: tuple[dict[str, Any], ...],
    phases: set[str],
) -> tuple[int, float] | None:
    from scripts.tick.deadline import Deadline

    eligible = _claimable_plans(client, slug)
    ids = dict.fromkeys(
        task_id for plan in plans if plan["id"] in eligible for task_id in (plan["id"], plan["enhances"])
    )
    replies = {task_id: resource.request(slug, f"tasks/{task_id}") for task_id in ids}
    pairs = backfill([reply["data"] for reply in replies.values()], phases, eligible)
    if not pairs:
        return None
    operations = [operation for pair in pairs for operation in pair]
    for operation in operations:
        if operation["op"] == "task_update":
            task_id = str(operation["item"]).removeprefix("tasks/")
            operation["expected_revision"] = replies[task_id]["revision"]
    started = time.monotonic()
    ledger.mutate(operations, deadline=Deadline.after(BACKFILL_BUDGET))
    return len(pairs), time.monotonic() - started


def main(argv: list[str] | None = None) -> int:
    from scripts.swarm.ledger_client import LedgerClient
    from scripts.tick.adapters.ledger import ClientLedger, resource_client

    parser = argparse.ArgumentParser(description="Backfill phases for claimable enhancement plans")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    client = LedgerClient()
    plans = _backfill_candidates(client, args.slug)
    before = len(plans)
    after = before
    moved = 0
    timings: list[float] = []
    if plans and not args.dry_run:
        clients = resource_client(args.slug)
        resource: Any = clients(BACKFILL_BUDGET)
        phases = {phase["id"] for phase in resource.collection(args.slug, "phases")}
        ledger = ClientLedger(args.slug, clients)
        for group in batched(plans, BACKFILL_PAIRS):
            if moved:
                time.sleep(BACKFILL_PAUSE)
            result = _backfill_batch(client, ledger, resource, args.slug, group, phases)
            if result is None:
                continue
            count, elapsed = result
            timings.append(elapsed)
            moved += count
            if elapsed > BACKFILL_MAX_WRITE:
                break
        after = len(_backfill_candidates(client, args.slug))
    print(json.dumps({"before": before, "moved": moved, "after": after, "batch_seconds": timings}))
    return int(not args.dry_run and (after > 0 or any(elapsed > BACKFILL_MAX_WRITE for elapsed in timings)))


if __name__ == "__main__":
    raise SystemExit(main())
