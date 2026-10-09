import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_queue_suites_use_a_precise_reuse_condition():
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    condition = "${{ !cancelled() && (github.event_name != 'merge_group' || needs.reuse.outputs.reused != 'true') }}"
    assert jobs["unit"].get("if") in (None, condition)
    if jobs["unit"].get("if") == condition:
        assert "reuse" in jobs["unit"]["needs"]
        assert jobs["reuse"]["outputs"]["reused"] == "${{ steps.decision.outputs.reused || 'false' }}"


@pytest.mark.parametrize("event", ["merge_group", "pull_request", "push", "workflow_dispatch"])
@pytest.mark.parametrize("reuse_result", ["success", "failure", "skipped"])
@pytest.mark.parametrize("baseline_result", ["success", "failure", "skipped"])
@pytest.mark.parametrize("source_run", ["12345", ""])
def test_required_gate_reuses_only_complete_queue_evidence(event, reuse_result, baseline_result, source_run):
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    step = jobs["gate-required"]["steps"][0]
    needs = {name: {"result": "skipped"} for name in jobs["gate-required"]["needs"]}
    needs["reuse"] = {"result": reuse_result, "outputs": {"reused": "true", "run": source_run}}
    needs["queue-baseline"] = {"result": baseline_result}
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(os.environ, NEEDS=json.dumps(needs), REUSED="true", EVENT=event, MUTATION="false"),
        capture_output=True,
        text=True,
    )
    expected = (
        "reuse" in jobs["gate-required"]["needs"]
        and event == "merge_group"
        and reuse_result == baseline_result == "success"
        and bool(source_run)
    )
    assert (result.returncode == 0) == expected, result.stdout + result.stderr


@pytest.mark.parametrize("failed", ["unit", "lint", "sonar", "test-count", "reuse", "queue-baseline"])
def test_a_reused_queue_rejects_failed_or_executed_suites(failed):
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    step = jobs["gate-required"]["steps"][0]
    needs = {name: {"result": "skipped"} for name in jobs["gate-required"]["needs"]}
    needs["reuse"] = {"result": "success", "outputs": {"reused": "true", "run": "12345"}}
    needs["queue-baseline"] = {"result": "success"}
    needs[failed]["result"] = "failure"
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(os.environ, NEEDS=json.dumps(needs), REUSED="true", EVENT="merge_group", MUTATION="false"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, result.stdout + result.stderr
