import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit


def _workflow():
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())


def test_required_gate_runs_after_parallel_unit_and_lint():
    jobs = _workflow()["jobs"]
    gate = jobs["gate-required"]
    assert gate["name"] == "Gate — Required"
    assert set(gate["needs"]) == {"unit", "lint"}
    assert gate["if"] == "${{ always() }}"
    assert "needs" not in jobs["unit"]
    assert "needs" not in jobs["lint"]
    assert "mutation" not in gate["needs"]


@pytest.mark.parametrize("unit", ["success", "failure", "skipped", "cancelled", "pending"])
@pytest.mark.parametrize("lint", ["success", "failure", "skipped", "cancelled", "pending"])
def test_required_gate_rejects_every_non_success_result(unit, lint):
    step = _workflow()["jobs"]["gate-required"]["steps"][0]
    assert step["env"]["NEEDS"] == "${{ toJSON(needs) }}"
    env = dict(os.environ, NEEDS=json.dumps({"unit": {"result": unit}, "lint": {"result": lint}}))
    result = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True)
    assert (result.returncode == 0) == (unit == lint == "success"), result.stdout + result.stderr
    if result.returncode:
        assert "::error::" in result.stdout


def test_passed_tree_lookup_skips_steps_without_skipping_required_jobs():
    jobs = _workflow()["jobs"]
    for name in ("unit", "lint"):
        job = jobs[name]
        assert "if" not in job
        lookup, *steps = job["steps"]
        assert lookup["if"] == "github.event_name == 'push'"
        assert all("steps.lookup.outputs.skip != 'true'" in step["if"] for step in steps)
    step = jobs["gate-required"]["steps"][0]
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(os.environ, NEEDS=json.dumps({"unit": {"result": "success"}, "lint": {"result": "success"}})),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_mutation_runs_in_tests_without_delaying_the_required_gate():
    workflow = _workflow()
    job = workflow["jobs"]["mutation"]
    assert "needs" not in job
    assert (
        job["if"]
        == "${{ (github.event_name == 'pull_request' && github.base_ref == 'dev') || github.event_name == 'workflow_dispatch' }}"
    )
    dispatch = workflow[True]["workflow_dispatch"]["inputs"]["base"]
    assert dispatch["required"] is True
    assert dispatch["default"] == "origin/dev"
    assert not (_ROOT / ".github/workflows/mutation.yml").exists()
