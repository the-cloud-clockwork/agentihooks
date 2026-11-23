"""The protected grader's acceptance of the Gate Required aggregation that requires the collect and lint-actions jobs."""

import copy
import json
import os
import subprocess
from typing import Any

import pytest

from scripts import ci_reuse, gate_protected
from scripts.ci_branch_lanes import GROUP

pytestmark = pytest.mark.unit

Workflow = dict[Any, Any]
Needs = dict[str, dict[str, Any]]


def _workflow() -> Workflow:
    return {
        "concurrency": {"group": GROUP, "queue": "max", "cancel-in-progress": False},
        True: {"pull_request": {"branches": ["dev"]}, "merge_group": None, "push": {"branches": ["dev"]}},
        "jobs": {
            "unit": {"runs-on": "ubuntu-latest"},
            "lint": {"runs-on": "ubuntu-latest"},
            "wiring": {"runs-on": "ubuntu-latest"},
            "gate-required": {
                "name": "Gate — Required",
                "needs": ["unit", "lint", "wiring"],
                "if": "${{ always() }}",
                "steps": ci_reuse.gate_steps(queue_tier=True, consolidated=True, promotion=True),
            },
        },
    }


def _passes(run: str, event: str, needs: Needs) -> bool:
    env = dict(os.environ, EVENT=event, NEEDS=json.dumps(needs), MUTATION="false", REUSED="false", KIND="false")
    return subprocess.run(["bash", "-e", "-c", run], env=env, capture_output=True, text=True).returncode == 0


@pytest.mark.parametrize("form", ["canonical", "unchanged", "weakened", "one job", "unwired"])
@pytest.mark.feature("gate-required-split-jobs")
def test_split_admission_requires_the_exact_reviewed_aggregation_and_both_jobs(form: str) -> None:
    base, head = _workflow(), _workflow()
    added = ["collect"] if form == "one job" else ["collect", "lint-actions"]
    for job in added:
        head["jobs"][job] = {"if": "github.event_name == 'pull_request'", "runs-on": "ubuntu-latest"}
    if form != "unwired":
        head["jobs"]["gate-required"]["needs"] += added
    if form != "unchanged":
        head["jobs"]["gate-required"]["steps"] = ci_reuse.split_required(head["jobs"]["gate-required"]["steps"])
    if form == "weakened":
        step = copy.deepcopy(head["jobs"]["gate-required"]["steps"][0])
        step["run"] = step["run"].replace('"lint-actions", ', "")
        head["jobs"]["gate-required"]["steps"] = [step]
    problems = gate_protected.grade({"test.yml": base}, {}, {"test.yml": head}, {})
    assert bool(problems) == (form != "canonical"), problems


@pytest.mark.parametrize("job", ["collect", "lint-actions"])
@pytest.mark.parametrize("status", ["success", "failure", "cancelled", "skipped", "missing"])
@pytest.mark.feature("gate-required-split-jobs")
def test_split_aggregation_requires_both_jobs_to_succeed_on_pull_requests(job: str, status: str) -> None:
    [step] = ci_reuse.split_required(ci_reuse.gate_steps(queue_tier=True, consolidated=True))
    required = ["select", "collect", "test-count", "lint", "lint-actions", "semgrep", "sonar"]
    needs: Needs = {name: {"result": "success"} for name in required}
    needs["select"]["outputs"] = {"scope": "none"}
    if status == "missing":
        del needs[job]
    else:
        needs[job] = {"result": status}
    assert _passes(step["run"], "pull_request", needs) == (status == "success")


@pytest.mark.parametrize(
    "event,collect,actions,expected",
    [
        ("merge_group", "success", "skipped", True),
        ("merge_group", "skipped", "skipped", False),
        ("merge_group", "failure", "skipped", False),
        ("merge_group", "success", "success", False),
        ("push", "skipped", "skipped", True),
        ("push", "failure", "skipped", False),
        ("push", "skipped", "failure", False),
    ],
)
@pytest.mark.feature("gate-required-split-jobs")
def test_split_aggregation_requires_collection_on_the_queue_and_skips_both_jobs_on_promotion(
    event: str, collect: str, actions: str, expected: bool
) -> None:
    [step] = ci_reuse.split_required(
        ci_reuse.lost_rerun(ci_reuse.gate_steps(queue_tier=True, consolidated=True, promotion=True, watcher_loss=True))
    )
    required = ["select", "split", "shard-check", "test-count"] if event == "merge_group" else ["select"]
    needs: Needs = {name: {"result": "success"} for name in required}
    needs["select"]["outputs"] = {"scope": "full"}
    if event == "merge_group":
        needs.update(
            {
                "unit": {"result": "success"},
                "unit-lost": {"result": "skipped"},
                "unit-rerun": {"result": "skipped"},
                "queue-fail-fast": {"result": "success"},
            }
        )
    needs["collect"] = {"result": collect}
    needs["lint-actions"] = {"result": actions}
    assert _passes(step["run"], event, needs) == expected


def test_split_aggregation_leaves_a_rendering_without_job_lists_unchanged() -> None:
    steps = ci_reuse.gate_steps()
    assert ci_reuse.split_required(steps) == steps
