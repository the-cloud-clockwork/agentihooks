"""Pure start and advance over a workflow definition; the caller saves the state as workflow.json."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from scripts.workflows.schema import EXIT, OUTCOMES, STAGE_PASS, Stage, Workflow, WorkflowError

RUNNING = "running"
PASSED = "passed"
EXHAUSTED = "exhausted"
LOOPS = ("retry", "enhance")

ExitCheck = Callable[[str, Any], bool]


def start(definition: Workflow) -> dict:
    return {
        "stage": definition.head,
        "status": RUNNING,
        "iterations": 0,
        "visit": 0,
        "runs": {},
        "outputs": {},
        "notes": None,
        "action": None,
    }


def advance(definition: Workflow, state: dict, stage: str, outcome: str, outputs: Any, exit_check: ExitCheck) -> dict:
    current = definition.stages[refused(state, stage, outcome)]
    after = copy.deepcopy(state)
    after["visit"] += 1
    after["runs"][stage] = after["runs"].get(stage, 0) + 1
    after["outputs"][stage] = copy.deepcopy(outputs)
    outcome = stage_outcome(current, after["visit"], outcome, outputs, exit_check)
    if workflow_exit(definition, stage, outcome, after["outputs"], exit_check):
        return finished(after, PASSED)
    if outcome is None:
        return after
    return follow(definition, current, after, outcome, outputs)


def refused(state: dict, stage: str, outcome: str) -> str:
    if state["status"] != RUNNING:
        raise WorkflowError([f"the workflow is {state['status']}"])
    if stage != state["stage"]:
        raise WorkflowError([f"{stage} is not the current stage {state['stage']}"])
    if outcome not in OUTCOMES:
        raise WorkflowError([f"{outcome} is not one of {', '.join(OUTCOMES)}"])
    return stage


def stage_outcome(current: Stage, visit: int, outcome: str, outputs: Any, exit_check: ExitCheck) -> str | None:
    if visit > current.iterations:
        raise WorkflowError([f"{current.id} already ran its {current.iterations} iterations"])
    if current.exit is None:
        return outcome
    if exit_check(current.exit, outputs):
        return "pass"
    return None if visit < current.iterations else outcome


def workflow_exit(definition: Workflow, stage: str, outcome: str | None, outputs: dict, exit_check: ExitCheck) -> bool:
    match = STAGE_PASS.match(definition.exit)
    if match:
        return match.group(1) == stage and outcome == "pass"
    return exit_check(definition.exit, outputs)


def follow(definition: Workflow, current: Stage, state: dict, outcome: str, outputs: Any) -> dict:
    target = current.next[outcome]
    if target is None:
        raise WorkflowError([f"{current.id} has no {outcome} edge"])
    if outcome in LOOPS:
        state["iterations"] += 1
        state["notes"] = {"stage": current.id, "outcome": outcome, "outputs": copy.deepcopy(outputs)}
        if state["iterations"] >= definition.max_iterations:
            state["action"] = definition.on_exhausted
            return finished(state, EXHAUSTED)
    if target == EXIT:
        return finished(state, PASSED)
    if target != current.id:
        state["visit"] = 0
    state["stage"] = target
    return state


def finished(state: dict, status: str) -> dict:
    state["status"] = status
    state["stage"] = None
    return state
