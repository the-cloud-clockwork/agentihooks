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
UNSLICED = (
    "plan task {task} enhances task {target}, but target {target} carries no plan slice: link its section with "
    "agentihooks ledger task set {target} plan_url=<link> plan_slice=<anchor>, then run swarm done again"
)
INCOMPLETE = "the plan section of task {target} is incomplete: {problems}"
CLEAN = "The slice check found no problems."
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


@dataclass(frozen=True)
class Closing:
    task: str
    target: str
    ask: str


def close(slug: str, row: dict[str, Any], autonomy: str) -> Closing | None:
    """The close of a plan task that enhances another, None for any other task. Refuses a target with no plan slice
    or an incomplete plan section; an empty ask approves the enhancement at full autonomy."""
    target_id = str(row.get("enhances") or "")
    if not target_id:
        return None
    doc, load = plan_read.fetched(slug, os.environ)
    tasks: list[dict[str, Any]] = doc["tasks"] if doc else []
    target = next((t for t in tasks if t["id"] == target_id), None)
    if doc is None or target is None or not (target.get("plan_slice") and target.get("plan_lines")):
        raise SwarmError(UNSLICED.format(task=row["id"], target=target_id))
    try:
        problems = thin_task.section_complete(plan_read.exact(doc, target, load))
    except ValueError as error:
        problems = [str(error)]
    if problems:
        raise SwarmError(INCOMPLETE.format(target=target_id, problems=" ".join(problems)))
    found = slice_check.task_problems(target, doc, slice_check.Limits.from_env(os.environ))
    if autonomy == FULL and not found:
        return Closing(row["id"], target_id, "")
    ask = RELAY if autonomy in (MANUAL, ASSIST) else REVIEW
    text = ask.format(task=row["id"], target=target_id, slug=slug, autonomy=autonomy, found=" ".join(found) or CLEAN)
    return Closing(row["id"], target_id, text)


def settle(ledger: Any, inbox: InboxStore, slug: str, by: str, closing: Closing | None) -> None:
    """After the plan task is done: an empty ask marks the target planned, any other ask goes to the master seat."""
    if closing is None:
        return
    if not closing.ask:
        ledger.update_task(slug, closing.target, {"enhancement": PLANNED}, by=by)
        return
    inbox.send(by, seat_address(slug, MASTER), closing.ask, task=closing.task)


def review(ledger: Any, slug: str, agent: AgentRecord, autonomy: str, decision: "Decision") -> dict[str, str]:
    """Approve or send back a closed enhancement: the master decides at delegate and full autonomy, the operator at
    manual and assist. A send back reopens the plan task with the note and keeps the target held."""
    row: dict[str, Any] = ledger.task(slug, decision.task) or {}
    target_id = str(row.get("enhances") or "")
    if not target_id:
        raise SwarmError(f"task {decision.task} enhances no task")
    target: dict[str, Any] = ledger.task(slug, target_id) or {}
    if row.get("state") != "done" or target.get("enhancement") != NEEDED:
        raise SwarmError(f"the enhancement of task {target_id} is not in review")
    if autonomy in (MANUAL, ASSIST):
        raise SwarmError(OPERATOR.format(autonomy=autonomy, target=target_id))
    if agent.lane != MASTER:
        raise SwarmError(f"only the swarm master reviews an enhancement at {autonomy} autonomy")
    if decision.action == "approve":
        ledger.update_task(slug, target_id, {"enhancement": PLANNED}, by=agent.name)
        return {"task": decision.task, "target": target_id, "state": "approved"}
    description = f"{row.get('description', '')}\n\nSent back: {decision.note}".lstrip()
    fields = {"state": "open", "claimed_by": "", "description": description}
    ledger.update_task(slug, decision.task, fields, by=agent.name)
    return {"task": decision.task, "target": target_id, "state": "sent_back"}
