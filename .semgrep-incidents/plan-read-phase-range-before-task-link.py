from collections.abc import Callable
from typing import Any


def stored_phase_first(phase: dict[str, Any], task: dict[str, Any]) -> dict[str, Any] | None:
    ref = phase.get("plan_ref")
    # ruleid: plan-read-phase-range-before-task-link
    if not ref and task and task.get("plan_url"):
        ref = {"artifact": task["plan_url"], "lines": task["plan_lines"]}
    return ref


def stored_task_link_first(
    phase: dict[str, Any], task: dict[str, Any], own: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None]
) -> dict[str, Any] | None:
    ref = own(phase, task) or phase.get("plan_ref")
    # ok: plan-read-phase-range-before-task-link
    if not ref and task and task.get("plan_url"):
        ref = {"artifact": task["plan_url"], "lines": task["plan_lines"]}
    return ref
