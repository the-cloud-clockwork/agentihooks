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
    folder.mkdir(parents=True)
    (folder / OUTPUTS_DIR).mkdir()
    state = {**machine.start(definition), "workflow": definition.name, "run": folder.name, "case": case_text}
    save(folder, state)
    return state


def load(folder: Path) -> dict:
    return json.loads((folder / STATE_FILE).read_text())


def save(folder: Path, state: dict) -> None:
    temp = folder / f".{STATE_FILE}.tmp"
    with temp.open("w") as handle:
        handle.write(json.dumps(state))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, folder / STATE_FILE)


def files(folder: Path) -> dict[str, str]:
    """Every regular file in the run folder as text, keyed by its path relative to the folder."""
    return {
        path.relative_to(folder).as_posix(): path.read_text(errors="replace")
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def restore(folder: Path, saved: dict[str, str]) -> None:
    """Write the saved files into the run folder; a path that leaves the folder is refused before any write."""
    root = folder.resolve()
    targets: dict[Path, str] = {}
    for name, text in saved.items():
        target = (root / name).resolve()
        if Path(name).is_absolute() or not target.is_relative_to(root):
            raise ValueError(f"run file {name} leaves the run folder")
        targets[target] = text
    for target, text in targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
