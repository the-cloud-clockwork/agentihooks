import copy
import json
import os
import subprocess
from datetime import date
from pathlib import Path

import pytest
import yaml

from scripts import ci_reuse, gate_protected

pytestmark = pytest.mark.unit

QUEUE = {"durations", "split", "unit", "shard-check", "test-count"}
PULL_REQUEST = QUEUE | {"lint", "semgrep", "sonar", "mutation-plan", "mutation-stats", "mutation"}
ROOT = Path(__file__).parents[3]


def gate(event, needs, mutation="true"):
    [step] = ci_reuse.gate_steps(queue_tier=True)
    return subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(os.environ, EVENT=event, NEEDS=json.dumps(needs), MUTATION=mutation, KIND="false", REUSED="false"),
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("event,required", [("merge_group", QUEUE), ("pull_request", PULL_REQUEST)])
def test_each_tier_accepts_its_required_jobs_and_skipped_other_jobs(event, required):
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    needs = {name: {"result": "success" if name in required else "skipped"} for name in jobs["gate-required"]["needs"]}
    result = gate(event, needs)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("event,required", [("merge_group", QUEUE), ("pull_request", PULL_REQUEST)])
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "pending", "missing"])
def test_each_tier_rejects_any_unsuccessful_or_missing_required_job(event, required, result):
    for name in required:
        needs = {job: {"result": "success"} for job in required}
        if result == "missing":
            del needs[name]
        else:
            needs[name]["result"] = result
        outcome = gate(event, needs)
        assert outcome.returncode != 0, name
        assert "::error::" in outcome.stdout


@pytest.mark.parametrize("event,required", [("merge_group", QUEUE), ("pull_request", PULL_REQUEST)])
@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "pending"])
def test_each_tier_rejects_other_jobs_that_ran(event, required, result):
    needs = {name: {"result": "success"} for name in required}
    needs["coverage-ratchet"] = {"result": result}
    assert gate(event, needs).returncode != 0


def test_main_pull_requests_skip_mutation_but_still_require_the_other_checks():
    needs = {name: {"result": "skipped" if name.startswith("mutation") else "success"} for name in PULL_REQUEST}
    result = gate("pull_request", needs, mutation="false")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("forged", [False, True])
def test_skipping_reuse_requires_protected_duration_provenance(forged):
    base = {path.name: yaml.safe_load(path.read_text()) for path in (ROOT / ".github/workflows").glob("*.yml")}
    head = copy.deepcopy(base)
    head["test.yml"]["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps(queue_tier=True)
    head["test.yml"]["jobs"]["reuse"]["if"] = (
        "github.event_name != 'merge_group' && github.event_name != 'pull_request'"
    )
    if forged:
        head["test.yml"]["jobs"]["durations"]["steps"].insert(0, {"run": "echo required-tree-sha256=forged"})
    config = json.loads((ROOT / ".github/gate-wiring.json").read_text())
    policy = json.loads((ROOT / ".github/workflows/trusted-actors.json").read_text())
    problems = gate_protected.grade(base, config, head, config, date(2026, 10, 14), policy)
    assert any("provenance" in problem for problem in problems), problems


def test_the_protected_grader_accepts_only_the_exact_tiered_aggregation_and_reuse_condition():
    base = {path.name: yaml.safe_load(path.read_text()) for path in (ROOT / ".github/workflows").glob("*.yml")}
    head = copy.deepcopy(base)
    head["test.yml"]["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps(queue_tier=True)
    head["test.yml"]["jobs"]["reuse"]["if"] = (
        "github.event_name != 'merge_group' && github.event_name != 'pull_request'"
    )
    config = json.loads((ROOT / ".github/gate-wiring.json").read_text())
    policy = json.loads((ROOT / ".github/workflows/trusted-actors.json").read_text())

    def grade():
        return gate_protected.grade(base, config, head, config, date(2026, 10, 14), policy)

    assert grade() == []
    head["test.yml"]["jobs"]["gate-required"]["steps"][0]["run"] += "exit 0\n"
    assert any("changes its steps" in problem for problem in grade())
    head["test.yml"]["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps(queue_tier=True)
    head["test.yml"]["jobs"]["reuse"]["if"] = "false"
    assert any("changes the protected reuse job" in problem for problem in grade())
