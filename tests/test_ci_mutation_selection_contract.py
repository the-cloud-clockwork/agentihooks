import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.ci_mutation import runner

_ROOT = Path(__file__).resolve().parents[1]


def _jobs():
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]


def _contract():
    steps = _jobs()["mutation-plan"]["steps"]
    return next(step for step in steps if step.get("id") == "contract")


def _check(tmp_path, selection):
    if selection is not None:
        (tmp_path / ".mutation-selection.json").write_text(selection)
    output, summary = tmp_path / "output", tmp_path / "summary"
    path = f"{Path(sys.executable).parent}:/usr/bin:/bin"
    assert shutil.which("python", path=path)
    result = subprocess.run(
        ["bash", "-e", "-c", _contract()["run"]],
        cwd=tmp_path,
        env={
            "PATH": path,
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        capture_output=True,
        text=True,
    )
    values = dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.is_file() else {}
    return result, values, summary.read_text() if summary.is_file() else ""


def test_an_older_head_without_a_selection_keeps_the_previous_contract_and_says_so(tmp_path):
    result, values, summary = _check(tmp_path, None)
    assert result.returncode == 0, result.stderr
    assert values == {"selection": "false"}
    assert "predates the planned test selection" in summary
    assert "matching and importing test modules" in summary


@pytest.mark.parametrize(
    "selection", ["{not json", "[]", '{"scripts/a.py": "tests/test_a.py"}', '{"scripts/a.py": [1]}']
)
def test_a_present_but_malformed_selection_still_fails(tmp_path, selection):
    result, values, _ = _check(tmp_path, selection)
    assert result.returncode != 0
    assert "malformed" in result.stdout + result.stderr
    assert values == {}


def test_a_shard_without_a_selection_selects_tests_as_before(tmp_path, monkeypatch):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/sample.py").write_text("def sample(value):\n    return value + 1\n")
    calls = []

    def select(root, path):
        calls.append((root, path))
        return ["tests/test_sample.py"]

    def mutate(root, work, selected, *args):
        assert selected == {"scripts/sample.py": ({2}, ["tests/test_sample.py"])}
        return {"scripts/sample.py": []}, ""

    monkeypatch.setattr(runner, "select_tests", select)
    monkeypatch.setattr(runner, "mutate_files", mutate)
    report = runner.run_gate(tmp_path, {"scripts/sample.py": {2}}, tmp_path / "output", 60)
    assert report["failed"] is False
    assert calls == [(tmp_path, Path("scripts/sample.py"))]


def test_a_well_formed_selection_is_published(tmp_path):
    result, values, summary = _check(tmp_path, json.dumps({"scripts/a.py": ["tests/test_a.py"], "scripts/b.py": []}))
    assert result.returncode == 0, result.stderr
    assert values == {"selection": "true"}
    assert summary == ""


def test_selection_upload_and_downloads_follow_the_contract():
    jobs = _jobs()
    plan = jobs["mutation-plan"]
    names = [step.get("name") for step in plan["steps"]]
    contract = _contract()
    assert contract["if"] == "steps.reuse.outputs.reused != 'true'"
    assert names.index("Size mutation shards from stored timings") < plan["steps"].index(contract)
    upload = plan["steps"][names.index("Upload planned mutation test selection")]
    assert plan["steps"].index(contract) < plan["steps"].index(upload)
    assert upload["if"] == "steps.reuse.outputs.reused != 'true' && steps.contract.outputs.selection == 'true'"
    assert upload["with"]["if-no-files-found"] == "error"
    assert plan["outputs"]["selection"] == "${{ steps.contract.outputs.selection || 'false' }}"
    for name in ("mutation-stats", "mutation"):
        download = next(
            step for step in jobs[name]["steps"] if step.get("name") == "Download planned mutation test selection"
        )
        assert download["if"] == (
            "needs.mutation-plan.outputs.forwarded != 'true' && needs.mutation-plan.outputs.selection == 'true'"
        )
