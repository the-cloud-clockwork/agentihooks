"""A phase's lifecycle, derived from stored facts only, so the tick, the CLI and the page cannot drift apart."""

from collections.abc import Callable
from functools import partial
from typing import Any

LABELS = {
    "out_of_scope": "Out of scope",
    "done": "Done",
    "to_plan": "Needs a plan",
    "planning": "Planning",
    "in_review": "Plan in review",
    "building": "Building",
}
WAITS = "Waits for phase "


def lifecycle(phase: dict[str, Any], doc: dict[str, Any]) -> str:
    return _lifecycle(phase, _done(doc.get("phases", [])), lambda: _plan_task(phase, doc))


def label(phase: dict[str, Any], doc: dict[str, Any]) -> str:
    state = lifecycle(phase, doc)
    if state != "waiting":
        return LABELS[state]
    return _waits(phase, {p["id"]: p for p in doc.get("phases", [])})


def status(doc: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Each phase's state and label, from one done set, one phase map and one plan task index for the whole doc."""
    phases = doc.get("phases", [])
    done, by_id = _done(phases), {p["id"]: p for p in phases}
    plans: dict[str, dict[str, Any]] | None = None

    def plan_task(phase: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal plans
        if plans is None:
            plans = _plan_tasks(doc["tasks"])
        return plans.get(phase["id"])

    found: dict[str, dict[str, str]] = {}
    for phase in phases:
        state = _lifecycle(phase, done, partial(plan_task, phase))
        found[phase["id"]] = {"state": state, "label": _waits(phase, by_id) if state == "waiting" else LABELS[state]}
    return found


def admits(task: dict[str, Any], doc: dict[str, Any]) -> bool:
    """Whether ``task``'s phase lets it be claimed: a plan task while the phase plans, an enhancement task while it
    plans or builds, and any other task while it builds."""
    phase = next((p for p in doc.get("phases", []) if p["id"] == task.get("phase")), None)
    if phase is None:
        return True
    state = lifecycle(phase, doc)
    if task.get("enhances"):
        return state in ("planning", "building")
    return state == ("planning" if task.get("kind") == "plan" else "building")


def report(doc):
    rows = []
    for phase in doc.get("phases", []):
        held = [
            t["id"]
            for t in doc["tasks"]
            if t.get("phase") == phase["id"]
            and t.get("state") == "open"
            and not t.get("out_of_scope")
            and not admits(t, doc)
        ]
        rows.append((phase["id"], lifecycle(phase, doc), held))
    return rows


def _plan_task(phase: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any] | None:
    """The phase's own plan task: its first plan kind task that enhances no other task."""
    return next((t for t in doc["tasks"] if t.get("phase") == phase["id"] and _own_plan(t)), None)


def _plan_tasks(tasks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each phase's own plan task, as _plan_task finds it, from one pass over the tasks."""
    found: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if _own_plan(task):
            found.setdefault(task.get("phase"), task)
    return found


def _own_plan(task: dict[str, Any]) -> bool:
    return task.get("kind") == "plan" and not task.get("enhances")


def _done(phases: list[dict[str, Any]]) -> set[str]:
    return {p["id"] for p in phases if p.get("done")}


def _lifecycle(phase: dict[str, Any], done: set[str], plan_task: Callable[[], dict[str, Any] | None]) -> str:
    if phase.get("out_of_scope"):
        return "out_of_scope"
    if phase.get("done"):
        return "done"
    if any(dep not in done for dep in phase.get("depends_on") or []):
        return "waiting"
    if phase.get("planning") != "auto":
        return "building"
    plan = plan_task()
    if plan is None:
        return "to_plan"
    if plan.get("state") != "done":
        return "planning"
    if (phase.get("review") or {}).get("state") != "approved":
        return "in_review"
    return "building"


def _waits(phase: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> str:
    waits = [dep for dep in phase["depends_on"] if not by_id.get(dep, {}).get("done")]
    return WAITS + ", ".join(by_id.get(dep, {}).get("title") or dep for dep in waits)
