"""Advance claimed workflow tasks in their workspaces and deliver head stages to their claimants."""

import json
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

import yaml

from scripts.inbox.store import InboxError
from scripts.workflows import exit_checks, loader, schema
from scripts.workflows.runner import StageLauncher, StageResult

machine: Any = import_module("scripts.workflows.machine")
store: Any = import_module("scripts.workflows.store")
runner: Any = import_module("scripts.workflows.runner")
HeadlessProfile: Any = import_module("scripts.workflows.launchers").HeadlessProfile
Judge: Any = import_module("scripts.workflows.launchers").Judge
Routed: Any = import_module("scripts.workflows.launchers").Routed


class PendingStage(Exception):
    """The claimant has not supplied this head stage's result yet."""


@dataclass(frozen=True)
class SeatLauncher:
    inbox: Any
    task: dict[str, Any]
    definition: schema.Workflow
    fallback: StageLauncher

    def launch(self, stage: schema.Stage, inputs: dict[str, Any], folder: Path) -> StageResult:
        if stage.id != self.definition.head:
            fallback: Any = self.fallback
            return fallback.launch(stage, inputs, folder)
        state = store.load(folder)
        generation = self.task.get("claim_generation", 0)
        result = folder / "outputs" / stage.id / f"seat-{generation}-{state['iterations']}-{state['visit']}.json"
        if result.is_file():
            found = json.loads(result.read_text())
            return StageResult(found["outcome"], found["outputs"])
        result.parent.mkdir(parents=True, exist_ok=True)
        receipt = result.with_suffix(".sent")
        claimant = self.task["claimed_by"]
        if not receipt.is_file() or receipt.read_text() != claimant:
            text = (
                f"Work the {stage.id} workflow stage for your claimed task {self.task['id']}. "
                f"Inputs: {json.dumps(inputs)}\n"
                f"Write the result to {result} as JSON with outcome (pass, retry or enhance) and outputs. "
                "The tick resumes the workflow after that result exists."
            )
            self.inbox.send("swarm", claimant, text, task=self.task["id"])
            receipt.write_text(claimant)
        raise PendingStage


def initialize(task: dict[str, Any], repo: Path, folder: Path) -> dict[str, Any]:
    if (folder / store.STATE_FILE).is_file():
        return store.load(folder)
    definitions, errors = loader.resolve(repo)
    if errors:
        raise ValueError("\n".join(errors))
    name = task["workflow"]
    if name not in definitions:
        raise ValueError(f"unknown workflow {name}")
    definition: Any = definitions[name].workflow
    case = Path(task["case"]).read_text() if task.get("case") else task["description"]
    folder.mkdir(parents=True, exist_ok=True)
    state = {
        **machine.start(definition),
        "workflow": name,
        "run": folder.name,
        "case": case,
        "definition": definition.to_document(),
        "harness": "auto",
    }
    store.save(folder, state)
    return state


def advance_task(inbox: Any, task: dict[str, Any], repo: Path) -> str:
    folder = Path(task["workspace"])
    state = initialize(task, repo, folder)
    if state["status"] != machine.RUNNING:
        return ""
    definition = schema.parse(yaml.safe_dump(state["definition"]))
    seat = SeatLauncher(inbox, task, definition, HeadlessProfile(harness=state["harness"]))
    launcher = Routed(seat, Judge(harness=state["harness"]))
    try:
        after = runner.Runner(definition, launcher, exit_checks.check).step(folder)
    except PendingStage:
        return f"workflow {task['id']} waits for its claimant"
    return f"workflow {task['id']} advanced from {state['stage']} to {after['stage']}: {after['status']}"


def advance_tasks(inbox: Any, doc: dict[str, Any], repo: Path) -> list[str]:
    actions: list[str] = []
    for task in doc.get("tasks", []):
        if (
            task.get("kind") != "workflow"
            or task.get("state") != "claimed"
            or task.get("done")
            or not task.get("claimed_by")
        ):
            continue
        try:
            action = advance_task(inbox, task, repo)
        except (ValueError, OSError, KeyError, InboxError) as exc:
            action = f"workflow {task['id']} refused: {exc}"
        if action:
            actions.append(action)
    return actions
