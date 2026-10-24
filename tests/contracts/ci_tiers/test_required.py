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

ROOT = Path(__file__).resolve().parents[3]
DURATION_JOB = (
    "durations"
    if "durations" in yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    else "select"
)
UNIT = {DURATION_JOB, "split", "unit", "shard-check", "test-count"}
QUEUE = UNIT | {"queue-fail-fast"}
PULL_REQUEST = UNIT | {
    "select",
    "lint",
    "semgrep",
    "sonar",
    "mutation-plan",
    "mutation-stats",
    "mutation",
    "writer-lock",
}
SELECTED = PULL_REQUEST - {"split", "unit", "shard-check"} | {"unit-selected"}
UNSELECTED = PULL_REQUEST - {"split", "unit", "shard-check"}
TIERS = [
    ("merge_group", QUEUE, ""),
    ("pull_request", PULL_REQUEST, "full"),
    ("pull_request", SELECTED, "selected"),
    ("pull_request", UNSELECTED, "none"),
]


def _gate_steps(*, docs=False):
    options = {"consolidated": True} if DURATION_JOB == "select" else {}
    return ci_reuse.postgres_required(ci_reuse.gate_steps(queue_tier=True, docs=docs, **options))


def gate(event, needs, mutation="true", scope="full"):
    if "select" in needs:
        needs = {**needs, "select": {**needs["select"], "outputs": {"scope": scope}}}
    [step] = _gate_steps()
    return subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(os.environ, EVENT=event, NEEDS=json.dumps(needs), MUTATION=mutation, KIND="false", REUSED="false"),
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("event,required,scope", TIERS)
def test_each_tier_accepts_its_required_jobs_and_skipped_other_jobs(event, required, scope):
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    needs = {name: {"result": "success" if name in required else "skipped"} for name in jobs["gate-required"]["needs"]}
    result = gate(event, needs, scope=scope)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("event,required,scope", TIERS)
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "pending", "missing"])
def test_each_tier_rejects_any_unsuccessful_or_missing_required_job(event, required, scope, result):
    for name in required:
        needs = {job: {"result": "success"} for job in required}
        if result == "missing":
            del needs[name]
        else:
            needs[name]["result"] = result
        outcome = gate(event, needs, scope=scope)
        assert outcome.returncode != 0, name
        assert "::error::" in outcome.stdout


@pytest.mark.parametrize("event,required,scope", TIERS)
@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "pending"])
@pytest.mark.parametrize("other", ["coverage-ratchet", "unit-selected", "unit"])
def test_each_tier_rejects_other_jobs_that_ran(event, required, scope, result, other):
    if other in required:
        return
    needs = {name: {"result": "success"} for name in required}
    needs[other] = {"result": result}
    assert gate(event, needs, scope=scope).returncode != 0


@pytest.mark.parametrize("scope", ["", "partial"])
def test_a_pull_request_without_a_known_selection_scope_is_red(scope):
    needs = {name: {"result": "success"} for name in PULL_REQUEST}
    assert gate("pull_request", needs, scope=scope).returncode != 0


def test_main_pull_requests_skip_mutation_but_still_require_the_other_checks():
    needs = {name: {"result": "skipped" if name.startswith("mutation") else "success"} for name in PULL_REQUEST}
    result = gate("pull_request", needs, mutation="false")
    assert result.returncode == 0, result.stdout + result.stderr


def test_duration_provenance_reads_the_base_and_records_the_queue_head():
    checkout, python, install, record, publish = ci_reuse.duration_provenance_steps()
    assert checkout["with"] == {
        "ref": "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha }}",
        "path": "provenance-grader",
        "fetch-depth": 0,
        "persist-credentials": False,
    }
    assert python["with"]["python-version"] == "3.12"
    assert install["working-directory"] == record["working-directory"] == "${{ github.workspace }}/provenance-grader"
    assert install["shell"] == record["shell"] == "bash"
    assert record["env"] == {
        "BASE": "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha }}",
        "HEAD": "${{ github.sha }}",
        "EVENT": "${{ github.event_name }}",
        "RUN": "${{ github.run_id }}",
        "ATTEMPT": "${{ github.run_attempt }}",
    }
    interpreter = '"$RUNNER_TEMP/provenance-python/bin/python" -I'
    assert f"{interpreter} -m pip install " in install["run"]
    assert f"{interpreter} scripts/ci_reuse.py " in record["run"]
    assert '--evidence "$RUNNER_TEMP/no-reuse"' in record["run"]
    assert publish["with"]["name"] == "required-tree-${{ github.run_attempt }}"
    assert publish["with"]["path"] == "${{ runner.temp }}/provenance.json"
    assert all(
        "github.event_name == 'merge_group'" in step["if"] and "github.event_name == 'pull_request'" in step["if"]
        for step in (checkout, python, install, record, publish)
    )


@pytest.mark.parametrize("forged", [False, True])
def test_skipping_reuse_requires_protected_duration_provenance(forged):
    base = {path.name: yaml.safe_load(path.read_text()) for path in (ROOT / ".github/workflows").glob("*.yml")}
    head = copy.deepcopy(base)
    head["test.yml"]["jobs"]["gate-required"]["steps"] = _gate_steps(
        docs="docs" in head["test.yml"]["jobs"]["gate-required"]["needs"]
    )
    head["test.yml"]["jobs"]["reuse"]["if"] = (
        "github.event_name != 'merge_group' && github.event_name != 'pull_request'"
    )
    prefix = ci_reuse.duration_provenance_steps()
    durations = head["test.yml"]["jobs"][DURATION_JOB]
    durations["steps"] = [step for step in durations["steps"] if step not in prefix]
    if forged:
        durations["steps"] = prefix + durations["steps"]
        durations["steps"].insert(0, {"run": "echo required-tree-sha256=forged"})
    config = json.loads((ROOT / ".github/gate-wiring.json").read_text())
    policy = json.loads((ROOT / ".github/workflows/trusted-actors.json").read_text())
    problems = gate_protected.grade(base, config, head, config, date(2026, 10, 14), policy)
    assert any("provenance" in problem for problem in problems), problems


def test_the_protected_grader_accepts_only_the_exact_tiered_aggregation_and_reuse_condition():
    base = {path.name: yaml.safe_load(path.read_text()) for path in (ROOT / ".github/workflows").glob("*.yml")}
    head = copy.deepcopy(base)
    head["test.yml"]["jobs"]["gate-required"]["steps"] = _gate_steps(
        docs="docs" in head["test.yml"]["jobs"]["gate-required"]["needs"]
    )
    head["test.yml"]["jobs"]["reuse"]["if"] = (
        "github.event_name != 'merge_group' && github.event_name != 'pull_request'"
    )
    head["test.yml"]["jobs"][DURATION_JOB]["steps"] = (
        ci_reuse.duration_provenance_steps() + head["test.yml"]["jobs"][DURATION_JOB]["steps"]
    )
    head["test.yml"]["jobs"][DURATION_JOB]["if"] = (
        "${{ !cancelled() && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') }}"
    )
    head["test.yml"]["jobs"][DURATION_JOB]["runs-on"] = "ubuntu-latest"
    config = json.loads((ROOT / ".github/gate-wiring.json").read_text())
    policy = json.loads((ROOT / ".github/workflows/trusted-actors.json").read_text())

    def grade():
        return gate_protected.grade(base, config, head, config, date(2026, 10, 14), policy)

    assert grade() == []
    head["test.yml"]["jobs"]["gate-required"]["steps"][0]["run"] += "exit 0\n"
    assert any("changes its steps" in problem for problem in grade())
    head["test.yml"]["jobs"]["gate-required"]["steps"] = _gate_steps(
        docs="docs" in head["test.yml"]["jobs"]["gate-required"]["needs"]
    )
    head["test.yml"]["jobs"]["reuse"]["if"] = "false"
    assert any("changes the protected reuse job" in problem for problem in grade())


@pytest.mark.parametrize(
    "field,value",
    [
        ("env", {"BASH_ENV": "forged.sh"}),
        ("defaults", {"run": {"shell": "python"}}),
        ("container", {"image": "example.invalid/forged:1.0"}),
        ("services", {"forger": {"image": "example.invalid/forged:1.0"}}),
        ("permissions", {"contents": "write"}),
        ("continue-on-error", True),
    ],
)
def test_duration_provenance_keeps_the_protected_execution_context(field, value):
    base = {path.name: yaml.safe_load(path.read_text()) for path in (ROOT / ".github/workflows").glob("*.yml")}
    head = copy.deepcopy(base)
    head["test.yml"]["jobs"]["reuse"]["if"] = (
        "github.event_name != 'merge_group' && github.event_name != 'pull_request'"
    )
    head["test.yml"]["jobs"][DURATION_JOB]["steps"] = (
        ci_reuse.duration_provenance_steps() + head["test.yml"]["jobs"][DURATION_JOB]["steps"]
    )
    head["test.yml"]["jobs"][DURATION_JOB][field] = value
    config = json.loads((ROOT / ".github/gate-wiring.json").read_text())
    policy = json.loads((ROOT / ".github/workflows/trusted-actors.json").read_text())
    problems = gate_protected.grade(base, config, head, config, date(2026, 10, 14), policy)
    assert any("provenance context" in problem for problem in problems), problems


@pytest.mark.parametrize(
    "field,value", [("env", {"BASH_ENV": "forged.sh"}), ("defaults", {"run": {"shell": "python"}})]
)
def test_duration_provenance_keeps_the_global_execution_context(field, value):
    base = {path.name: yaml.safe_load(path.read_text()) for path in (ROOT / ".github/workflows").glob("*.yml")}
    head = copy.deepcopy(base)
    head["test.yml"]["jobs"]["reuse"]["if"] = (
        "github.event_name != 'merge_group' && github.event_name != 'pull_request'"
    )
    head["test.yml"]["jobs"][DURATION_JOB]["steps"] = (
        ci_reuse.duration_provenance_steps() + head["test.yml"]["jobs"][DURATION_JOB]["steps"]
    )
    head["test.yml"][field] = value
    config = json.loads((ROOT / ".github/gate-wiring.json").read_text())
    policy = json.loads((ROOT / ".github/workflows/trusted-actors.json").read_text())
    problems = gate_protected.grade(base, config, head, config, date(2026, 10, 14), policy)
    assert any("global provenance context" in problem for problem in problems), problems


@pytest.mark.parametrize("missing", ["base-job", "head-job", "steps"])
def test_missing_duration_definitions_are_reported_without_trusting_the_producer(missing):
    base = {path.name: yaml.safe_load(path.read_text()) for path in (ROOT / ".github/workflows").glob("*.yml")}
    head = copy.deepcopy(base)
    head["test.yml"]["jobs"]["reuse"]["if"] = (
        "github.event_name != 'merge_group' && github.event_name != 'pull_request'"
    )
    head["test.yml"]["jobs"][DURATION_JOB]["steps"] = (
        ci_reuse.duration_provenance_steps() + head["test.yml"]["jobs"][DURATION_JOB]["steps"]
    )
    if missing == "base-job":
        del base["test.yml"]["jobs"][DURATION_JOB]
    elif missing == "head-job":
        del head["test.yml"]["jobs"][DURATION_JOB]
    else:
        del head["test.yml"]["jobs"][DURATION_JOB]["steps"]
    config = json.loads((ROOT / ".github/gate-wiring.json").read_text())
    policy = json.loads((ROOT / ".github/workflows/trusted-actors.json").read_text())
    problems = gate_protected.grade(base, config, head, config, date(2026, 10, 14), policy)
    assert any("provenance" in problem for problem in problems), problems


@pytest.mark.parametrize("field,value", [("env", {"BASH_ENV": "trusted.sh"}), ("defaults", {"run": {"shell": "bash"}})])
def test_duration_provenance_accepts_unchanged_global_execution_context(field, value):
    base = {path.name: yaml.safe_load(path.read_text()) for path in (ROOT / ".github/workflows").glob("*.yml")}
    base["test.yml"][field] = value
    head = copy.deepcopy(base)
    head["test.yml"]["jobs"]["reuse"]["if"] = (
        "github.event_name != 'merge_group' && github.event_name != 'pull_request'"
    )
    head["test.yml"]["jobs"][DURATION_JOB]["steps"] = (
        ci_reuse.duration_provenance_steps() + head["test.yml"]["jobs"][DURATION_JOB]["steps"]
    )
    config = json.loads((ROOT / ".github/gate-wiring.json").read_text())
    policy = json.loads((ROOT / ".github/workflows/trusted-actors.json").read_text())
    assert gate_protected.grade(base, config, head, config, date(2026, 10, 14), policy) == []
