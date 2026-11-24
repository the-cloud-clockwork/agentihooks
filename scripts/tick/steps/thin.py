"""The thin task pass: queue one planner enhancement task for each open engineer or ci task whose text is too thin
to build from, mark the task as needing enhancement in the same ledger write and tell the master once."""

from collections.abc import Callable
from itertools import takewhile
from typing import Any

from scripts.swarm import freeze, thin_task
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm_ledger import ledger_comments
from scripts.swarm_ledger.repository import hierarchy
from scripts.swarm_ledger.repository.live_rows import ancestry
from scripts.tick import registry
from scripts.tick.deadline import Deadline
from scripts.tick.ports.contracts import Ledger
from scripts.tick.ports.tools import Tools
from scripts.tick.step import Lane
from scripts.tick.steps import CallStep, hook_domains

Get = Callable[[str], Any]
SENDER = "swarm"
LANES = ("eng", "ci")
NEEDED = {"enhancement": "needed"}
TITLE = "Plan a thin task before an engineer claims it"
DISTINCT = "Each thin task gets its own enhancement plan task, keyed by the task it enhances."


def _thin_task_pass(tick: Any, get: Get) -> list[str]:
    """Queue an enhancement plan task for each thin open engineer or ci task with an empty enhancement field that no
    freeze holds until the step deadline is spent, then tell the master about every task this pass queued."""
    tools: Tools = get("tools")
    deadline: Deadline = get("deadline")
    doc: dict[str, Any] = get("doc")
    tasks: list[dict[str, Any]] = doc["tasks"]
    ids = {task["id"] for task in tasks}
    lines: list[str] = []
    queued: list[str] = []
    for task in takewhile(lambda _: not deadline.expired, filter(_unfrozen(doc), filter(unchecked, tasks))):
        if problems := thin_task.thin({"description": "", **task}):
            line, marked = enhance(tools.ledger, deadline, task, problems, ids)
            lines.append(line)
            queued += [task["id"]] if marked else []
    if queued:
        get("inbox").send(SENDER, f"master@{get('slug')}", _told(queued), fyi=True)
    return lines


def _unfrozen(doc: dict[str, Any]) -> Callable[[dict[str, Any]], bool]:
    """Return the check that no freeze record of the ledger ``doc`` holds a task, as the swarm freeze check reads it
    over the ledger hierarchy projected once for the pass."""
    records: list[dict[str, Any]] = doc.get("freezes") or []
    graph: dict[str, Any] = hierarchy.project(doc)[0]  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return lambda task: not freeze.frozen(task, records, ancestry(task, graph))


def unchecked(task: dict[str, Any]) -> bool:
    """Return whether ``task`` is an open, in scope engineer or ci task, a missing lane reading as engineer, that no
    thin check has marked."""
    in_lane = task.get("lane", "eng") in LANES and not task.get("out_of_scope")
    return task.get("state") == "open" and in_lane and not task.get("enhancement")


def enhance(
    ledger: Ledger, deadline: Deadline, task: dict[str, Any], problems: list[str], ids: set[str]
) -> tuple[str, bool]:
    """Add the enhancement task of the thin ``task`` unless ``ids`` holds it and mark the task as needing enhancement,
    both in one ledger write; return the action line and whether the write landed."""
    tid, enhancement = task["id"], thin_task.ENHANCE.format(task["id"])
    added = [] if enhancement in ids else [_addition(task, problems)]
    try:
        ledger.mutate([*added, _marking(task)], deadline=deadline)
    except LedgerRefused as exc:
        return f"refused the enhancement of {tid}: {exc}", False
    return f"queued {enhancement} for thin task {tid}", True


def _marking(task: dict[str, Any]) -> dict[str, object]:
    """Return the enhancement operation guarded by the task's observed enhancement."""
    tid = task["id"]
    return {
        "op": "task_enhancement",
        "id": f"thin-mark-{tid}",
        "by": SENDER,
        "item": f"tasks/{tid}",
        "expected": task.get("enhancement"),
        **NEEDED,
    }


def _addition(task: dict[str, Any], problems: list[str]) -> dict[str, object]:
    """Return the task add operation of the plan lane task that enhances ``task``, in its phase when it has one."""
    operation: dict[str, object] = {
        "op": "task_add",
        "id": f"thin-add-{task['id']}",
        "by": SENDER,
        "task": thin_task.ENHANCE.format(task["id"]),
        "title": _title(task),
        "lane": "plan",
        "kind": "plan",
        "description": _brief(task, problems),
        "enhances": task["id"],
        "not_duplicate": DISTINCT,
    }
    return operation | ({"phase": task["phase"]} if task.get("phase") else {})


def _title(task: dict[str, Any]) -> str:
    """Return the enhancement task title naming the target's title, or the plain title when the target has none or
    the ledger would refuse the named one."""
    title = task.get("title")
    named = f"Plan the thin task {title}"
    return named if title and not ledger_comments.problems(named, "item") else TITLE


def _brief(task: dict[str, Any], problems: list[str]) -> str:
    """Return the enhancement task description: the rules ``task`` fails and its current description."""
    return " ".join(
        [
            f"Write a full plan section for task {task['id']} before an engineer claims it.",
            "It fails these rules:",
            *problems,
            "Its current description:",
            task.get("description") or "none.",
        ]
    )


def _told(queued: list[str]) -> str:
    """Return the master notice naming each enhancement task this pass queued."""
    names = ", ".join(thin_task.ENHANCE.format(tid) for tid in queued)
    return (
        f"For your information: the thin task check queued {names} for a planner; "
        "each enhanced task is held from engineer and ci claims until its plan closes."
    )


registry.register(
    CallStep(
        "thin task pass",
        "wrapper phases",
        _thin_task_pass,
        needs=frozenset(),
        reads=hook_domains("doc"),
        writes=frozenset(),
        lane=Lane("tick"),
        effects=frozenset({"inbox", "ledger"}),
        feature="tick-thin-task-pass",
    )
)
