"""Why the tick may not claim an open task: its phase, a freeze or focus, or an open dependency.

Every claim filter asks here, so the urgent hold health finding reports exactly what keeps a task from a seat.
"""

from time import time

from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import freeze, phase_state
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


def hold(task: dict, rows: dict, doc: dict, graph: dict, fix: str) -> str:
    return verification(task) or phased(task, doc) or frozen(task, doc, graph, fix) or waits(task, rows)


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
        if (task.get("contract") or {}).get("after_at"):
            continue
        task_id = task["id"]
        mark = stamps.get(f"tasks/{task_id}/awaiting", {}).get("at") or 0
        since_key = store.key(slug, f"verification-hold:{task_id}")
        store.redis.set(since_key, mark or at, nx=True)
        since = max(int(store.redis.get(since_key)), mark)
        notice_key = store.key(slug, f"verification-notice:{task_id}")
        last = store.redis.get(notice_key)
        if at - since <= 3_600_000 or (last is not None and at - int(last) < 86_400_000):
            continue
        text = (
            f"Task {task_id} ({task.get('title') or task_id}) awaits verification without a proof window "
            "for more than an hour. Set contract.after_at to its proof time, or reopen it with "
            f"agentihooks ledger --slug {slug} --as master task set {task_id} awaiting="
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
    if not freeze.held(task, doc, graph, fix):
        return ""
    return freezes(task, doc, graph) or FOCUS


def freezes(task: dict, doc: dict, graph: dict) -> str:
    chain = freeze.ancestry(task, graph)
    for record in doc.get("freezes") or []:
        if record["verb"] == "freeze" and freeze.covers(record["target"], task, chain):
            name = freeze.names({**doc, "freezes": [record]})[0]
            return f"{name} holds it"
    return ""


def urgent(task: dict, rows: dict, doc: dict, graph: dict) -> str:
    return phased(task, doc) or freezes(task, doc, graph) or waits(task, rows)


def waits(task: dict, rows: dict) -> str:
    if task_dependencies.ready(task, rows):
        return ""
    named = dict.fromkeys((task.get("parked_on") or []) + (task.get("depends_on") or []))
    open_ = [dep for dep in named if rows.get(dep, {}).get("state") != "done"]
    return f"it waits on task {open_[0]}" if len(open_) == 1 else f"it waits on tasks {', '.join(open_)}"
