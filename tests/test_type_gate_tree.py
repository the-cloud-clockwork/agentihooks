import json
import subprocess
from pathlib import Path

import pytest

from scripts import type_gate

pytestmark = pytest.mark.unit

ILL_TYPED = "def planted(value: int) -> str:\n    return value\n"
_SETTINGS = "\n".join(f"{key} = {json.dumps(value)}" for key, value in type_gate.CONFIG.items())
PYPROJECT = f"[tool.basedpyright]\n{_SETTINGS}\n"


def _tree(root: Path, files: dict[str, str], baseline: dict[str, int] | None = None) -> Path:
    for name, text in {"pyproject.toml": PYPROJECT, "hooks/__init__.py": "", **files}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if baseline is not None:
        (root / "tests").mkdir(exist_ok=True)
        (root / type_gate.BASELINE).write_text(json.dumps(baseline))
    return root


def _fake_checker(monkeypatch: pytest.MonkeyPatch, diagnostics: list[dict[str, str]]) -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, f"basedpyright {type_gate.BASEDPYRIGHT_VERSION}\n", "")
        files = command[command.index("--pythonpath") + 2 :]
        matched = [diagnostic for diagnostic in diagnostics if diagnostic["file"] in files]
        report = {"generalDiagnostics": matched, "summary": {"filesAnalyzed": len(files)}}
        return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

    monkeypatch.setattr(type_gate.subprocess, "run", run)


def test_a_file_above_its_tree_baseline_entry_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _tree(tmp_path, {"hooks/planted.py": ILL_TYPED}, {"hooks/planted.py": 0})
    _fake_checker(monkeypatch, [{"file": str(head / "hooks/planted.py"), "severity": "error"}])
    code = type_gate.main(["--tree", "--head", str(head)])
    out = capsys.readouterr().out
    assert code == 1
    assert "::error::hooks/planted.py has 1 type errors, more than the 0 the base allows\n" in out


def test_a_file_with_no_baseline_entry_above_zero_errors_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _tree(tmp_path, {"hooks/planted.py": ILL_TYPED}, {})
    _fake_checker(monkeypatch, [{"file": str(head / "hooks/planted.py"), "severity": "error"}])
    code = type_gate.main(["--tree", "--head", str(head)])
    out = capsys.readouterr().out
    assert code == 1
    assert "::error::hooks/planted.py has 1 type errors, more than the 0 the base allows\n" in out


def test_a_file_at_or_below_its_entry_is_green_with_no_drift_or_rewrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _tree(tmp_path, {"hooks/planted.py": ILL_TYPED}, {"hooks/planted.py": 2, "hooks/other.py": 5})
    _fake_checker(monkeypatch, [{"file": str(head / "hooks/planted.py"), "severity": "error"}])
    code = type_gate.main(["--tree", "--head", str(head)])
    out = capsys.readouterr().out
    assert (code, out) == (0, "1 type errors in 1 files, 7 allowed, 0 errors\n")
    assert "write" not in out


def test_every_graded_source_is_measured_with_no_base_needed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _tree(tmp_path, {"hooks/a.py": "", "scripts/b.py": "", "tests/c.py": ""}, {})
    checked: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, f"basedpyright {type_gate.BASEDPYRIGHT_VERSION}\n", "")
        files = command[command.index("--pythonpath") + 2 :]
        checked.append(files)
        report = {"generalDiagnostics": [], "summary": {"filesAnalyzed": len(files)}}
        return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

    monkeypatch.setattr(type_gate.subprocess, "run", run)
    code = type_gate.main(["--tree", "--head", str(head)])
    assert code == 0
    assert checked == [type_gate.sources(head)]
    assert capsys.readouterr().out == "0 type errors in 0 files, 0 allowed, 0 errors\n"
