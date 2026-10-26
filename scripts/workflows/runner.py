"""Drives machine.advance through a launcher interface; resume is run on an existing folder."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from scripts.workflows.machine import ExitCheck
from scripts.workflows.schema import Stage, Workflow


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
        raise NotImplementedError

    def run(self, folder: Path) -> dict:
        raise NotImplementedError
