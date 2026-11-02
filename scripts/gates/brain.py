"""The brain gate: a swarm worker's covered write needs a consultation receipt bound to its task's current revision.

The gate checks the receipt the last delivered task context left; it never queries the brain itself.
"""

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping
from typing import Any, cast

from scripts.gates.base import Call, Decision, Who
from scripts.gates.build import EDIT_TOOLS, SERENA, SERENA_EDITS

PURPOSE = "task"
SPEC = ("title", "description", "plan_slice", "plan_lines", "phase")
DELIVERED, EMPTY, FAILED = "delivered", "empty", "failed"
CONSULTED = frozenset({DELIVERED, EMPTY})
NO_RECEIPT = "no task context was delivered to this session"
UNRELATED = "the receipt answers another purpose than this task"
OTHER_TASK = "the receipt belongs to another task"
STALE_GENERATION = "the receipt predates the current claim of this task"
STALE_REVISION = "the task spec changed after the receipt"
OTHER_BRAIN = "the receipt came from another brain"
EXPIRED = "the receipt expired"
FAILED_RESULT = "the brain failed to answer, so nothing was consulted"
UNREADABLE = "the task spec could not be read"
REFUSAL = (
    "brain gate: {reason} for task {task}; the next brain refresh delivers the current task context, then write again"
)


def task_row(ledger_dir: str, slug: str, task: str) -> dict[str, Any]:
    import scripts.swarm_ledger.storage

    doc = (
        cast(
            Callable[[str, str, str], dict[str, Any] | None],
            scripts.swarm_ledger.storage.read_ledger,
        )(ledger_dir, slug, f"tasks/{task}")
        or {}
    )
    tasks = cast(list[dict[str, Any]], doc.get("tasks", []))
    empty: dict[str, Any] = {}
    return next((t for t in tasks if t.get("id") == task), empty)


def bind(task: str, row: dict[str, Any], brain: str) -> dict[str, Any]:
    spec = json.dumps({name: row.get(name, "") for name in SPEC}, sort_keys=True)
    return {
        "purpose": PURPOSE,
        "task": task,
        "revision": hashlib.sha256(spec.encode()).hexdigest(),
        "generation": row.get("claim_generation", 0),
        "brain": brain,
    }


def check(receipt: dict[str, Any] | None, current: dict[str, Any], now: float) -> str:
    if not receipt:
        return NO_RECEIPT
    if receipt.get("purpose") != PURPOSE:
        return UNRELATED
    if receipt.get("task") != current["task"]:
        return OTHER_TASK
    if receipt.get("generation") != current["generation"]:
        return STALE_GENERATION
    if receipt.get("revision") != current["revision"]:
        return STALE_REVISION
    if receipt.get("brain") != current["brain"]:
        return OTHER_BRAIN
    if receipt.get("expires", 0) <= now:
        return EXPIRED
    return "" if receipt.get("result") in CONSULTED else FAILED_RESULT


class BrainGate:
    name = "brain"
    default_mode = "observe"

    def __init__(
        self,
        environ: dict[str, str] | None = None,
        receipts: Callable[[str], dict[str, Any] | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.environ = environ
        self.receipts = receipts
        self.clock = clock

    def matches(self, call: Call) -> bool:
        if call.tool in EDIT_TOOLS:
            return True
        return call.tool.startswith(SERENA) and call.tool[len(SERENA) :] in SERENA_EDITS

    def decide(self, call: Call, who: Who, state: object) -> Decision:
        if not (who.swarm and who.task):
            return Decision()
        from hooks.context import task_context

        current = cast(
            Callable[[str, str, Mapping[str, str]], dict[str, Any] | None],
            task_context.binding,
        )(who.swarm, who.task, os.environ if self.environ is None else self.environ)
        receipt = (self.receipts or cast(Callable[[str], dict[str, Any] | None], task_context.consultation))(
            call.session
        )
        reason = check(receipt, current, self.clock()) if current else UNREADABLE
        return Decision.deny(REFUSAL.format(reason=reason, task=who.task)) if reason else Decision()
