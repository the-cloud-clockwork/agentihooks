"""Drives machine.advance through a launcher interface; resume is run on an existing folder."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from scripts.workflows import machine, store
from scripts.workflows.machine import ExitCheck
from scripts.workflows.schema import Stage, Workflow

__all__ = ["Runner", "StageLauncher", "StageResult"]


@dataclass(frozen=True)
class StageResult:
    outcome: str
    outputs: Any


class StageLauncher(Protocol):
    def launch(self, stage: Stage, inputs: dict, folder: Path) -> StageResult: ...


@dataclass(frozen=True)
class Runner:
    definition: Workflow
    launcher: StageLauncher
    exit_check: ExitCheck

    def step(self, folder: Path) -> dict:
        state = store.load(folder)
        if state["status"] != machine.RUNNING:
            return state
        stage_id = state["stage"]
        stage = self.definition.stages[stage_id]
        inputs = stage_inputs(stage, state)
        result = self.launcher.launch(stage, inputs, folder)
        after = machine.advance(self.definition, state, stage_id, result.outcome, result.outputs, self.exit_check)
        store.save(folder, after)
        return after

    def run(self, folder: Path) -> dict:
        state = self.step(folder)
        while state["status"] == machine.RUNNING:
            state = self.step(folder)
        return state


def stage_inputs(stage: Stage, state: dict) -> dict:
    inputs = {"case": state["case"]}
    for stage_id in stage.input:
        if stage_id in state["outputs"]:
            inputs[stage_id] = state["outputs"][stage_id]
    return inputs
