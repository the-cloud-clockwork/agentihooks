"""Screens task adds against the tasks the ledger already holds, before the locked apply takes the write lock."""

import os
import threading
from dataclasses import dataclass, field

from scripts.swarm_ledger import ledger_duplicates

BUDGET_S = float(os.environ.get("LEDGER_DUPLICATE_BUDGET_S", "7"))
TICK = "swarm"
MINTED = ("plan", "release")
REFUSAL = (
    'task {new} repeats task {id} "{title}" ({state}, rank {rank}, phase {phase}): {fate}. '
    'If it is a different change, add it with --not-duplicate "<why it differs>"'
)
UNCHECKED = "the duplicate check did not run for task {new} because {why}, so it was added unchecked"
UNANSWERED = "the classifier did not answer"
SLOW = "it took longer than {budget:g} seconds"


@dataclass(frozen=True)
class Screen:
    refused: dict = field(default_factory=dict)
    warnings: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Gate:
    screen: Screen
    inner: object = None

    def apply(self, doc: dict, op: dict, ctx: object, apply_op: object) -> bool:
        if refusal := self.screen.refused.get(op["id"]):
            ctx.refused.append(refusal)
            return False
        return self.inner.apply(doc, op, ctx, apply_op) if self.inner else apply_op(doc, op, ctx)


def screen(doc: dict, ops: list[dict]) -> Screen:
    adds = [op for op in ops if checked(op)]
    if not adds:
        return Screen()
    found, why = _find(doc, adds)
    refused, warnings = {}, {}
    for op, match in zip(adds, found):
        if match == ledger_duplicates.UNCHECKED:
            warnings[op["id"]] = UNCHECKED.format(new=op["task"], why=why)
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
    result = [[ledger_duplicates.UNCHECKED] * len(adds)]

    def run():
        result[0] = ledger_duplicates.find(doc, "task", adds)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(BUDGET_S)
    if worker.is_alive():
        return [ledger_duplicates.UNCHECKED] * len(adds), SLOW.format(budget=BUDGET_S)
    return result[0], UNANSWERED
