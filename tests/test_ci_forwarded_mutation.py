import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def _jobs():
    path = Path(__file__).resolve().parents[1] / ".github/workflows/test.yml"
    return yaml.safe_load(path.read_text())["jobs"]


def test_forwarded_mutation_statistics_never_requests_a_runner():
    condition = _jobs()["mutation-stats"]["if"]
    assert "needs.mutation-plan.result == 'success'" in condition
    assert "&& needs.mutation-plan.outputs.forwarded != 'true'" in condition


@pytest.mark.parametrize("event", ["pull_request", "workflow_dispatch"])
@pytest.mark.parametrize("forwarded", ["true", "false", ""])
@pytest.mark.parametrize("plan", ["success", "failure", "skipped", "cancelled"])
@pytest.mark.parametrize("stats", ["success", "failure", "skipped", "cancelled"])
def test_required_gate_grades_forwarded_reports_and_real_statistics(event, forwarded, plan, stats):
    step = _jobs()["gate-required"]["steps"][0]
    required = ["select", "test-count", "lint", "docs", "semgrep", "sonar", "writer-lock"]
    needs = {name: {"result": "success"} for name in required}
    needs["select"]["outputs"] = {"scope": "none"}
    needs["mutation-plan"] = {"result": plan, "outputs": {"forwarded": forwarded}}
    needs["mutation-stats"] = {"result": stats}
    needs["mutation"] = {"result": "skipped"}
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(os.environ, NEEDS=json.dumps(needs), EVENT=event, MUTATION="true", INLINE="true"),
        capture_output=True,
        text=True,
    )
    passes = plan == "success" and (stats == "success" or (stats == "skipped" and forwarded == "true"))
    assert (result.returncode == 0) == passes, result.stdout + result.stderr
