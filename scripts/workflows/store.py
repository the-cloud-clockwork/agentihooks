"""The run folder: workflow.json plus outputs/, saved so a crash never leaves a partial file."""

from __future__ import annotations

import json
import os
from pathlib import Path

from hooks import config
from scripts.workflows import machine
from scripts.workflows.schema import Workflow

STATE_FILE = "workflow.json"
OUTPUTS_DIR = "outputs"


def default_root() -> Path:
    return Path(config.AGENTIHOOKS_HOME) / "workflows" / "runs"


def create(folder: Path, definition: Workflow, case_text: str) -> dict:
    folder.mkdir(parents=True, exist_ok=False)
    (folder / OUTPUTS_DIR).mkdir()
    state = {**machine.start(definition), "workflow": definition.name, "run": folder.name, "case": case_text}
    save(folder, state)
    return state


def load(folder: Path) -> dict:
    return json.loads((folder / STATE_FILE).read_text())


def save(folder: Path, state: dict) -> None:
    temp = folder / f".{STATE_FILE}.tmp"
    temp.write_text(json.dumps(state))
    os.replace(temp, folder / STATE_FILE)
