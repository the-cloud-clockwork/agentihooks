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
    required = {"unit", "lint", "sonar", "mutation", "test-count", "semgrep"}
    assert gate["name"] == "Gate — Required"
    assert (
        required
        <= set(gate["needs"])
        <= required
        | {"swarm-image", "shard-check", "brain-smoke", "wiring", "size", "dependency-audit", "durations", "helm-kind"}
    )
    assert gate["if"] == "${{ always() }}"
    assert jobs["unit"]["needs"] == ["durations"]
    assert "needs" not in jobs["lint"]
    if "swarm-image" in gate["needs"]:
        assert jobs["swarm-image"]["uses"] == "./.github/workflows/swarm-smoke.yml"


def test_semgrep_grades_registry_pack_findings_new_against_the_base_in_parallel():
    job = _workflow()["jobs"]["semgrep"]
    assert "needs" not in job
    assert job["uses"] == "./.github/workflows/semgrep.yml"
    assert job["with"]["base"] == (
        "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha"
        " || github.event.before || inputs.base }}"
    )
    scan = yaml.safe_load((_ROOT / ".github/workflows/semgrep.yml").read_text())["jobs"]["scan"]
    assert scan["steps"][0]["with"]["fetch-depth"] == 0
    command = scan["steps"][-1]["run"].split()
    assert {"p/ci", "p/secrets", "p/python"} == {command[i + 1] for i, a in enumerate(command) if a == "--config"}
    assert command[command.index("--baseline-commit") + 1] == '"$BASE"'
    assert {"--error", "--strict", "--verbose"} <= set(command)
    assert int(command[command.index("--timeout") + 1]) >= 30
    assert command[:2] == ["semgrep", "scan"]
    assert not [s for s in ("||", "&&", ";", "exit") if s in scan["steps"][-1]["run"]]
    assert not {"set", "--exclude", "--include"} & set(command)
    assert [command[i + 1] for i, a in enumerate(command) if a == "--exclude-rule"] == [
        "yaml.github-actions.security.github-actions-mutable-action-tag.github-actions-mutable-action-tag"
    ]
    assert all("if" not in step and "continue-on-error" not in step for step in scan["steps"])
    assert not {"if", "continue-on-error"} & (set(scan) | set(job))


def test_no_workflow_run_script_embeds_an_expression_semgrep_cannot_parse():
    paths = sorted((_ROOT / ".github").glob("workflows/*.yml")) + sorted(
        (_ROOT / ".github").glob("actions/**/action.yml")
    )
    embedded = []
    for path in paths:
        document = yaml.safe_load(path.read_text())
        steps = [step for job in document.get("jobs", {}).values() for step in job.get("steps", [])]
        steps += document.get("runs", {}).get("steps", [])
        embedded += [(path.name, step.get("name")) for step in steps if "${{" in step.get("run", "")]
    assert len(paths) > 10
    assert embedded == []


def test_unit_matrix_does_not_fail_fast():
    assert _workflow()["jobs"]["unit"]["strategy"]["fail-fast"] is False


def test_test_count_floor_runs_per_suite_beside_unit_against_the_base():
    job = _workflow()["jobs"]["test-count"]
    assert "needs" not in job
    assert (
        job["strategy"]["matrix"]["python-version"]
        == _workflow()["jobs"]["unit"]["strategy"]["matrix"]["python-version"]
    )
    base, floor = job["steps"][-2:]
    assert base["env"]["BASE"] == (
        "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha"
        " || github.event.before || inputs.base }}"
    )
    assert base["run"] == 'git worktree add --detach "$RUNNER_TEMP/base" "$BASE"'
    assert floor["run"] == (
        'if [[ ! -f "$RUNNER_TEMP/base/tests/count_floor.py" ]]; then\n'
        '  echo "::error::The base carries no tests/count_floor.py, so nothing trusted can grade."\n'
        "  exit 1\n"
        "fi\n"
        'cd "$RUNNER_TEMP/base"\n'
        'python -m tests.count_floor --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"\n'
    )


def test_test_count_refuses_a_base_without_its_grader(tmp_path):
    floor = _workflow()["jobs"]["test-count"]["steps"][-1]
    (tmp_path / "base").mkdir()
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), GITHUB_WORKSPACE=str(tmp_path))
    result = subprocess.run(["bash", "-e", "-c", floor["run"]], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "nothing trusted can grade" in result.stdout


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


@pytest.mark.parametrize("durations", ["success", "failure", "skipped", "cancelled"])
def test_required_gate_is_red_when_the_durations_lookup_did_not_succeed(durations):
    gate = _workflow()["jobs"]["gate-required"]
    assert "durations" in gate["needs"]
    needs = {"durations": {"result": durations}, "unit": {"result": "skipped" if durations != "success" else "success"}}
    env = dict(os.environ, NEEDS=json.dumps(needs), MUTATION="false")
    result = subprocess.run(["bash", "-e", "-c", gate["steps"][0]["run"]], env=env, capture_output=True, text=True)
    assert (result.returncode == 0) == (durations == "success"), result.stdout + result.stderr


@pytest.mark.parametrize("mutation", ["success", "failure", "skipped", "cancelled"])
@pytest.mark.parametrize("expected", ["true", "false", ""])
def test_required_gate_is_red_unless_mutation_passed_or_was_not_due(mutation, expected):
    jobs = _workflow()["jobs"]
    gate = jobs["gate-required"]
    step = gate["steps"][0]
    assert jobs["mutation"]["if"].startswith("${{")
    assert step["env"]["MUTATION"] == jobs["mutation"]["if"]
    needs = {name: {"result": "success"} for name in ("unit", "lint", "sonar")}
    needs["mutation"] = {"result": mutation}
    env = dict(os.environ, NEEDS=json.dumps(needs), MUTATION=expected)
    result = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True)
    passes = mutation == "success" or (mutation == "skipped" and expected == "false")
    assert (result.returncode == 0) == passes, result.stdout + result.stderr
    if result.returncode:
        assert "::error::" in result.stdout


def test_unit_and_lint_run_on_every_event_and_feed_the_required_gate():
    jobs = _workflow()["jobs"]
    for name in ("unit", "lint"):
        job = jobs[name]
        assert "if" not in job
        assert all("steps.lookup" not in step.get("if", "") for step in job["steps"])
    step = jobs["gate-required"]["steps"][0]
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(os.environ, NEEDS=json.dumps({"unit": {"result": "success"}, "lint": {"result": "success"}})),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_mutation_runs_in_tests_beside_unit_and_lint():
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
