"""Source trees and a recording basedpyright stand-in for the type gate's run tests."""

import json
import subprocess
from pathlib import Path

import pytest

from scripts import type_gate


def source_tree(root: Path, count: int) -> list[str]:
    """Write ``count`` empty test sources under ``root`` and return their sorted paths."""
    files = [f"tests/test_{index:04}.py" for index in range(count)]
    (root / "tests").mkdir()
    for path in files:
        (root / path).write_text("")
    return files


def record_checks(monkeypatch: pytest.MonkeyPatch, root: Path) -> list[list[str]]:
    """Replace basedpyright with a checker that reports one error per source it checks, and return its runs."""
    runs: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, f"basedpyright {type_gate.BASEDPYRIGHT_VERSION}\n", "")
        checked = command[command.index("--pythonpath") + 2 :]
        runs.append(checked)
        diagnostics = [{"file": str(root / path), "severity": "error"} for path in checked]
        report = {"generalDiagnostics": diagnostics, "summary": {"filesAnalyzed": len(checked)}}
        return subprocess.CompletedProcess(command, 1, json.dumps(report), "")

    monkeypatch.setattr(type_gate.subprocess, "run", run)
    return runs
