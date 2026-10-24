"""Workflow tasks: the fields a named workflow gives task add, and the proof its workspace state gives done."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.workflows import loader

STATE = "workflow.json"
VERDICT = "verdict.json"
EXHAUSTED = "workflow exhausted"


def add_fields(name: str, case: str, lane: str, profile: str | None, cwd: Path) -> dict:
    found, _ = loader.resolve(cwd)
    if name not in found:
        raise ValueError(f"unknown workflow {name}")
    if lane != "eng":
        raise ValueError("workflow tasks use the eng lane")
    workflow = found[name].workflow
    fields = {"workflow": name, "profile": profile or workflow.stages[workflow.head].profile}
    if case:
        path = Path(case).resolve()
        if not path.is_file():
            raise ValueError(f"case not found: {case}")
        fields["case"] = str(path)
    return fields


def read(workspace: str, name: str) -> dict:
    if not workspace:
        return {}
    try:
        value = json.loads((Path(workspace) / name).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def proof(workspace: str) -> dict:
    return {
        "workflow": str(read(workspace, STATE).get("status", "")),
        "verdict": str(read(workspace, VERDICT).get("verdict", "")),
    }


def exhausted_notes(workspace: str) -> str | None:
    state = read(workspace, STATE)
    if state.get("status") != "exhausted":
        return None
    return str(state.get("last_notes") or EXHAUSTED)
