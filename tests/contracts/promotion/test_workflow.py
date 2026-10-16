from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import ci_budget
from tests.contracts.ci_tiers.test_workflow import enabled

ROOT = Path(__file__).resolve().parents[3]


def test_promotion_requires_all_push_checks_and_never_runs_for_a_red_grade():
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    promote = workflow["jobs"]["promote"]
    assert promote["uses"] == "./.github/workflows/promote.yml"
    assert {"gate-required", "refresh-durations", "coverage-baseline", "flake-budget"} <= set(promote["needs"])
    assert "github.event_name == 'push'" in promote["if"]
    assert "github.ref == 'refs/heads/dev'" in promote["if"]
    assert "needs.gate-required.result == 'success'" in promote["if"]
    assert "!contains(needs.*.result, 'failure')" in promote["if"]
    assert "!contains(needs.*.result, 'cancelled')" in promote["if"]


def _graded_needs(promote, analysis="A1"):
    return {
        name: SimpleNamespace(result="success", outputs=SimpleNamespace(analysis=analysis)) for name in promote["needs"]
    }


@pytest.mark.parametrize("result", ["failure", "cancelled"])
@pytest.mark.parametrize("job", ["gate-required", "refresh-durations", "coverage-baseline", "flake-budget", "sonar"])
def test_a_red_or_cancelled_promotion_dependency_prevents_all_promotion_writes(job, result):
    promote = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["promote"]
    needs = _graded_needs(promote)
    assert enabled(promote["if"], "push", needs)
    needs[job].result = result
    assert not enabled(promote["if"], "push", needs)


def test_a_revision_sonar_graded_promotes_from_its_own_analysis():
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    promote = workflow["jobs"]["promote"]
    assert "sonar" in promote["needs"]
    assert workflow["jobs"]["sonar"]["outputs"] == {"analysis": "${{ steps.gate.outputs.analysis }}"}
    assert enabled(promote["if"], "push", _graded_needs(promote))


def test_a_superseded_revision_sonar_never_graded_is_not_promoted():
    promote = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["promote"]
    assert not enabled(promote["if"], "push", _graded_needs(promote, analysis=""))


def test_promotion_reuses_the_release_environment_and_never_dispatches_or_publishes_to_pypi():
    workflow = yaml.safe_load((ROOT / ".github/workflows/promote.yml").read_text())
    assert "workflow_call" in workflow.get("on", workflow.get(True))
    job = workflow["jobs"]["promote"]
    assert job["environment"] == "release"
    assert job["timeout-minutes"] == 10
    assert ci_budget.BUDGETS["promote"] == job["timeout-minutes"] * 60
    assert job["concurrency"] == {"group": "release-version", "cancel-in-progress": False}
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "--generate-notes" in commands
    assert "--verify-tag" in commands
    assert "workflow run" not in commands
    assert "publish-pypi" not in commands
    mint = next(step for step in job["steps"] if step.get("id") == "app-token")
    assert mint["with"]["permission-metadata"] == "read"
    assert mint["with"]["permission-contents"] == "write"
    assert mint["with"]["permission-pull-requests"] == "write"
