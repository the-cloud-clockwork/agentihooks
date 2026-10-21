"""Why the tick may not claim an open task: its phase, a freeze or focus, or an open dependency.

Every claim filter asks here, so the urgent hold health finding reports exactly what keeps a task from a seat.
"""

from time import time

from scripts.swarm import freeze, phase_state
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
