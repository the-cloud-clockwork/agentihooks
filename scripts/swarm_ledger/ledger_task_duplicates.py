"""Screens task adds against the tasks the ledger already holds, before the locked apply takes the write lock."""

import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as Overrun
from dataclasses import dataclass, field

from scripts.swarm_ledger import ledger_duplicates

BUDGET_S = float(os.environ.get("LEDGER_DUPLICATE_BUDGET_S", "5"))
WORKERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ledger-duplicates")
TICK = "swarm"
MINTED = ("plan", "release")
REFUSAL = (
    'task {new} repeats task {id} "{title}" ({state}, rank {rank}, phase {phase}): {fate}. '
    'If it is a different change, add it with --not-duplicate "<why it differs>"'
)
UNCHECKED_PREFIX = "the duplicate check did not run"
UNCHECKED_WARNING = UNCHECKED_PREFIX + " for task {new} because {why}, so it was added unchecked"
UNANSWERED = "the classifier did not answer"
SLOW = "it took longer than {budget:g} seconds"
FAILED = "it failed with {error}"


@dataclass(frozen=True)
class Screen:
    refused: dict = field(default_factory=dict)
    warnings: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Gate:
    screen: Screen
    inner: object

    def apply(self, doc: dict, op: dict, ctx: object, apply_op: object) -> bool:
        refusal = self.screen.refused.get(op["id"])
        return self.inner.apply(doc, op, ctx, _refuse(refusal) if refusal else apply_op)


def screen(doc: dict, ops: list[dict]) -> Screen:
    adds = [op for op in ops if checked(op)]
    if not adds:
        return Screen()
    found, why = _find(doc, adds)
    refused, warnings = {}, {}
    for op, match in zip(adds, found):
        if match == ledger_duplicates.UNCHECKED:
            warnings[op["id"]] = UNCHECKED_WARNING.format(new=op["task"], why=why)
        elif match is not None:
            refused[op["id"]] = refusal(op, match)
    return Screen(refused, warnings)


def checked(op: dict) -> bool:
    if op["op"] != "task_add" or op.get("not_duplicate"):
        return False
    return not (op["by"] == TICK and op["task"] in {f"{kind}-{op.get('phase', '')}" for kind in MINTED})


def refusal(op: dict, match: ledger_duplicates.Match) -> str:
    fate = "it is already built" if match.built else "it is on the ledger and will be built"
    return REFUSAL.format(
        new=op["task"],
        id=match.id,
        title=match.title,
        state=match.state,
        rank=match.rank,
        phase=match.phase_title or "none",
        fate=fate,
    )


def _find(doc, adds):
    future = WORKERS.submit(ledger_duplicates.find, doc, "task", adds)
    try:
        return future.result(timeout=BUDGET_S), UNANSWERED
    except Overrun:
        future.cancel()
        why = SLOW.format(budget=BUDGET_S)
    except Exception as exc:  # a broken check lets the add land, named in its warning
        why = FAILED.format(error=type(exc).__name__)
    return [ledger_duplicates.UNCHECKED] * len(adds), why


def _refuse(refusal):
    def refuse(doc, op, ctx):
        ctx.refused.append(refusal)
        return False

    return refuse
