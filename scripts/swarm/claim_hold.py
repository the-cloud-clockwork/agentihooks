"""Why the tick may not claim an open task: its phase, a freeze or focus, an open dependency, a thin task waiting for
its enhancement plan, or a lab behind on grading.

Every claim filter asks here, so the urgent hold health finding reports exactly what keeps a task from a seat.
"""

from collections.abc import Callable
from functools import partial
from time import time
from typing import Any

from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import freeze, phase_state, thin_task
from scripts.swarm.store import MASTER
from scripts.swarm_ledger import task_dependencies

PHASES = {
    "waiting": "waiting on an earlier phase",
    "to_plan": "waiting for its planner",
    "planning": "being planned",
    "in_review": "waiting for its plan review",
    "building": "building",
    "done": "done",
    "out_of_scope": "out of scope",
}
FOCUS = "a focus elsewhere holds it"
LAB_LANE = "eng"
LAB_CLASS = "pull_request"
LAB_FRESH_MS = 600_000
LAB_BEHIND = "the lab is {minutes} minutes behind on pull request grading"
THIN_LANES = ("eng", "ci")
THIN = "it waits for its enhancement plan task {}"


def hold(task: dict[str, Any], rows: dict[str, Any], doc: dict[str, Any], graph: dict[str, Any], fix: str) -> str:
    return verification(task) or phased(task, doc) or frozen(task, doc, graph, fix) or waits(task, rows) or thin(task)


def holds(
    task: dict[str, Any],
    rows: dict[str, Any],
    doc: dict[str, Any],
    graph: dict[str, Any],
    fix: str,
    admitted_queue: Any = freeze.UNCACHED,
) -> bool:
    """Whether ``hold`` names a reason, without building the freeze reason text."""
    return (
        bool(verification(task) or phased(task, doc))
        or freeze.claim_gate(task, doc, graph, fix, admitted_queue)  # pyright: ignore[reportUnknownMemberType]
        or bool(waits(task, rows) or thin(task))
    )


def thin(task: dict[str, Any]) -> str:
    """Why the thin check holds an eng or ci task back: it needs enhancement until its plan task closes and marks it
    planned, even once the planner has given it a plan slice, unless it carries a pull request link of earlier work."""
    if task.get("enhancement") != "needed" or task.get("pr_url") or task.get("lane", LAB_LANE) not in THIN_LANES:
        return ""
    return THIN.format(thin_task.ENHANCE.format(task["id"]))


def verification(task: dict) -> str:
    if task.get("awaiting") != "verify":
        return ""
    at = (task.get("contract") or {}).get("after_at")
    if not at or int(at) > time() * 1000:
        return "it waits for its post merge proof window or a master reopen"
    return ""


def verification_notices(slug: str, store: object, doc: dict, at: int) -> list[str]:
    actions = []
    stamps = doc.get("_meta", {}).get("stamps", {})
    for task in doc["tasks"]:
        if task.get("state") != "open" or task.get("out_of_scope") or task.get("awaiting") != "verify":
            continue
        task_id = task["id"]
        since_key = store.key(slug, f"verification-hold:{task_id}")
        if (task.get("contract") or {}).get("after_at"):
            store.redis.delete(since_key)
            continue
        mark = max(stamps.get(f"tasks/{task_id}/{field}", {}).get("at") or 0 for field in ("awaiting", "contract"))
        store.redis.set(since_key, mark or at, nx=True)
        since = max(int(store.redis.get(since_key)), mark)
        notice_key = store.key(slug, f"verification-notice:{task_id}")
        last = store.redis.get(notice_key)
        if at - since <= 3_600_000 or (last is not None and at - int(last) < 86_400_000):
            continue
        text = (
            f"Task {task_id} ({task.get('title') or task_id}) awaits verification without a proof window "
            "for more than an hour. Set contract.after_at to its proof time, or reopen it with "
            f"agentihooks api --slug {slug} --as master task set {task_id} awaiting="
        )
        InboxStore(store.redis).send("swarm", seat_address(slug, MASTER), text)
        store.redis.set(notice_key, at)
        actions.append(f"told the master about verification hold {task_id}")
    return actions


def phased(task: dict, doc: dict) -> str:
    if phase_state.admits(task, doc):
        return ""
    phase = next(p for p in doc["phases"] if p["id"] == task.get("phase"))
    return f"its phase {phase.get('title') or phase['id']} is {PHASES[phase_state.lifecycle(phase, doc)]}"


def frozen(task: dict, doc: dict, graph: dict, fix: str) -> str:
    if not freeze.claim_gate(task, doc, graph, fix):
        return ""
    return freezes(task, doc, graph) or FOCUS


def freezes(task: dict, doc: dict, graph: dict) -> str:
    chain = freeze.ancestry(task, graph)
    for record in doc.get("freezes") or []:
        if record["verb"] == "freeze" and freeze.covers(record["target"], task, chain):
            name = freeze.names({**doc, "freezes": [record]})[0]
            return f"{name} holds it"
    return ""


def urgent(task: dict[str, Any], rows: dict[str, Any], doc: dict[str, Any], graph: dict[str, Any], fix: str) -> str:
    return phased(task, doc) or frozen(task, doc, graph, fix) or waits(task, rows) or thin(task)


def waits(task: dict, rows: dict) -> str:
    if task_dependencies.ready(task, rows):
        return ""
    named = dict.fromkeys((task.get("parked_on") or []) + (task.get("depends_on") or []))
    open_ = [dep for dep in named if rows.get(dep, {}).get("state") != "done"]
    return f"it waits on task {open_[0]}" if len(open_) == 1 else f"it waits on tasks {', '.join(open_)}"


def lab_held(task: dict[str, Any], fix: str) -> bool:
    """Whether the lab hold reaches the task: an eng task outside the fix phase."""
    return task.get("lane", LAB_LANE) == LAB_LANE and not (fix and task.get("phase") == fix)


def wait_minutes(wait_s: float | None) -> int:
    """Whole minutes of a predicted wait, zero when the lab predicts none."""
    return 0 if wait_s is None else max(int(wait_s) // 60, 0)


def lab_minutes(reading: dict[str, Any] | None, now_ms: int) -> int:
    """Whole minutes a lab reading taken within the fresh window predicts pull request grading waits; zero for an
    older, failed or missing reading."""
    if not reading or now_ms - reading.get("at", 0) > LAB_FRESH_MS:
        return 0
    return wait_minutes(reading["capacity"]["classes"].get(LAB_CLASS, {}).get("predicted_wait_s"))


def ci_behind(task: dict[str, Any], reading: dict[str, Any] | None, limit: int, fix: str) -> str:
    """Why the lab holds the task from a claim: its pull request grading runs more than ``limit`` minutes behind;
    empty when it does not or the task is outside the hold."""
    minutes = lab_minutes(reading, int(time() * 1000))
    if minutes <= limit or not lab_held(task, fix):
        return ""
    return LAB_BEHIND.format(minutes=minutes)


def lab_reason(store: Any, slug: str, fix: str) -> Callable[[dict[str, Any]], str]:
    """Each task's ``ci_behind`` text against the swarm's stored lab reading and the lab threshold."""
    from scripts.swarm import lab_capacity
    from scripts.swarm.health import findings

    return partial(ci_behind, reading=lab_capacity.status(store, slug), limit=findings.ci_behind_minutes(), fix=fix)
