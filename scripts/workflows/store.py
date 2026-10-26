"""The run folder: workflow.json plus outputs/, saved so a crash never leaves a partial file."""

from __future__ import annotations

from pathlib import Path

from scripts.workflows.schema import Workflow


def default_root() -> Path:
    raise NotImplementedError


def create(folder: Path, definition: Workflow, case_text: str) -> dict:
    raise NotImplementedError


def load(folder: Path) -> dict:
    raise NotImplementedError


def save(folder: Path, state: dict) -> None:
    raise NotImplementedError
