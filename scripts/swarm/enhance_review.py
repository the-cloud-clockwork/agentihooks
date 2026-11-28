"""The enhancement close: a plan task that enhances a thin task closes only on a complete plan section for its target,
and the target's enhancement turns planned at full autonomy or when the seat that decides approves it."""

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from scripts.inbox.seats import seat_address  # pyright: ignore[reportUnknownVariableType]
from scripts.inbox.store import InboxStore
from scripts.swarm import slice_check, thin_task
from scripts.swarm.store import ASSIST, FULL, MANUAL, MASTER, AgentRecord, SwarmError
from scripts.swarm_ledger import plan_read

if TYPE_CHECKING:
    from scripts.swarm.plan_review import Decision

NEEDED = "needed"
PLANNED = "planned"
CLEAN = "The slice check found no problems."
UNSLICED = (
    "plan task {task} enhances task {target}, but target {target} carries no plan slice: link its section with "
    "agentihooks ledger task set {target} plan_url=<link> plan_slice=<anchor>, then run swarm done again"
)
INCOMPLETE = "the plan section of task {target} is incomplete: {problems}"
REVIEW = (
    "Plan task {task} closed the enhancement of task {target}, which stays held from claims until it is approved. "
    "Review its plan section, then run agentihooks swarm {slug} plan approve --task {task} or agentihooks swarm "
    '{slug} plan send-back --task {task} --note "<what to change>". {found}'
)
RELAY = (
    "Plan task {task} closed the enhancement of task {target}, which stays held from claims until it is approved. "
    "Relay it to the operator: the operator decides it at {autonomy} autonomy, approving it by setting the "
    "enhancement of task {target} to planned. {found}"
)
OPERATOR = (
    "at {autonomy} autonomy the operator decides the enhancement of task {target}: relay it to the operator, who "
    "approves it by setting its enhancement to planned"
)
UNTARGETED = "task {task} enhances no task"
NOT_IN_REVIEW = "the enhancement of task {target} is not in review"
MASTER_ONLY = "only the swarm master reviews an enhancement at {autonomy} autonomy"
SENT_BACK = "{description}\n\nSent back: {note}"


@dataclass(frozen=True)
class Closing:
    task: str
    target: str
    faults: str
    autonomy: str


def close(slug: str, row: dict[str, Any], autonomy: str) -> Closing | None:
    """The close of a plan task that enhances another, None for any other task. Refuses a target with no plan slice
    or an incomplete plan section."""
    target_id = row.get("enhances")
    if not target_id:
        return None
    doc, load = plan_read.fetched(slug, os.environ)
    target = next((t for t in doc["tasks"] if t["id"] == target_id), None) if doc else None
    if doc is None or target is None or not target.get("plan_lines"):
        raise SwarmError(UNSLICED.format(task=row["id"], target=target_id))
    try:
        text = plan_read.exact(doc, target, load)
    except ValueError as error:
        raise SwarmError(INCOMPLETE.format(target=target_id, problems=str(error)))
    problems = thin_task.section_complete(text)
    if problems:
        raise SwarmError(INCOMPLETE.format(target=target_id, problems=" ".join(problems)))
    limits = slice_check.Limits.from_env(os.environ)
    faults = " ".join(slice_check.task_problems(target, doc, limits))
    return Closing(row["id"], target_id, faults, autonomy)


def settle(ledger: Any, inbox: InboxStore, slug: str, by: str, closing: Closing | None) -> None:
    """After the plan task is done: full autonomy marks a target the slice check clears planned, any other close asks
    the master seat."""
    if closing is None:
        return
    if closing.autonomy == FULL and not closing.faults:
        ledger.set_enhancement(slug, closing.target, PLANNED, by)
        return
    inbox.send(by, seat_address(slug, MASTER), ask(slug, closing))


def ask(slug: str, closing: Closing) -> str:
    found = closing.faults or CLEAN
    if closing.autonomy in (MANUAL, ASSIST):
        return RELAY.format(task=closing.task, target=closing.target, autonomy=closing.autonomy, found=found)
    return REVIEW.format(task=closing.task, target=closing.target, slug=slug, found=found)


def review(ledger: Any, slug: str, agent: AgentRecord, autonomy: str, decision: "Decision") -> dict[str, str]:
    """Approve or send back a closed enhancement: the master decides at delegate and full autonomy, the operator at
    manual and assist. A send back reopens the plan task with the note and keeps the target held."""
    row: dict[str, Any] = ledger.task(slug, decision.task) or {}
    target_id = row.get("enhances")
    if not target_id:
        raise SwarmError(UNTARGETED.format(task=decision.task))
    target: dict[str, Any] = ledger.task(slug, target_id) or {}
    if row.get("state") != "done" or target.get("enhancement") != NEEDED:
        raise SwarmError(NOT_IN_REVIEW.format(target=target_id))
    if autonomy in (MANUAL, ASSIST):
        raise SwarmError(OPERATOR.format(autonomy=autonomy, target=target_id))
    if agent.lane != MASTER:
        raise SwarmError(MASTER_ONLY.format(autonomy=autonomy))
    if decision.action == "approve":
        ledger.set_enhancement(slug, target_id, PLANNED, agent.name)
        return {"task": row["id"], "target": target_id, "state": "approved"}
    description = SENT_BACK.format(description=row["description"], note=decision.note)
    ledger.update_task(slug, row["id"], {"state": "open", "claimed_by": "", "description": description}, by=agent.name)
    return {"task": row["id"], "target": target_id, "state": "sent_back"}
