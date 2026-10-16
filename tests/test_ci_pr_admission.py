from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


@pytest.fixture
def jobs():
    root = Path(__file__).resolve().parents[1]
    return yaml.safe_load((root / ".github/workflows/test.yml").read_text())["jobs"]


@pytest.mark.parametrize("command", ["ruff check", "ruff format --check", "scripts.size_limits"])
def test_pull_request_selection_waits_for_each_cheap_check(jobs, command):
    steps = jobs["select"]["steps"]
    selection = next(i for i, step in enumerate(steps) if step.get("id") == "select")
    gates = [step for step in steps[:selection] if command in step.get("run", "")]
    assert len(gates) == 1, f"Selection must wait for {command}"
    assert gates[0].get("if") == "github.event_name == 'pull_request'"
    assert not gates[0].get("continue-on-error", False)
    assert "|| true" not in gates[0]["run"]
    assert "if" not in steps[selection]


def test_both_unit_routes_wait_for_the_checked_selection(jobs):
    for name in ("split", "unit", "unit-selected"):
        assert "select" in jobs[name]["needs"]
        assert "needs.select.outputs.scope" in jobs[name]["if"]
    assert jobs["select"]["needs"] == ["reuse", "admission"]


def test_size_admission_uses_the_protected_grader_and_actual_base(jobs):
    steps = jobs["select"]["steps"]
    size = next(step for step in steps if "scripts.size_limits" in step.get("run", ""))
    assert 'cd "$RUNNER_TEMP/grader"' in size["run"]
    assert '--base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"' in size["run"]
    base = next(step for step in steps if '"$RUNNER_TEMP/base"' in step.get("run", "") and step is not size)
    assert base["env"]["BASE"] == "${{ github.event.pull_request.base.sha }}"
    assert steps.index(base) < steps.index(size)
