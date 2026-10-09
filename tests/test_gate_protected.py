import copy
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest
import yaml

from scripts import gate_protected

_ROOT = Path(__file__).resolve().parents[1]
_TODAY = date(2026, 10, 9)
pytestmark = pytest.mark.unit


def _workflow():
    return {
        True: {"pull_request": {"branches": ["dev"]}, "merge_group": None, "push": {"branches": ["dev"]}},
        "jobs": {
            "unit": {"runs-on": "ubuntu-latest"},
            "lint": {"runs-on": "ubuntu-latest"},
            "wiring": {"runs-on": "ubuntu-latest"},
            "gate": {
                "name": "Gate — Required",
                "needs": ["unit", "lint", "wiring"],
                "if": "${{ always() }}",
                "steps": [{"run": "jq -e 'all(.[]; .result == \"success\")'"}],
            },
        },
    }


def _grade(head, base=None, head_config=None, base_config=None):
    base = base or {"test.yml": _workflow()}
    return gate_protected.grade(base, base_config or {}, head, head_config or {}, _TODAY)


def _entry():
    return {"reason": "why", "owner": "ci-1@swarm", "expires": "2026-12-01"}


def test_an_unchanged_head_is_green():
    assert _grade({"test.yml": _workflow()}) == []


def test_a_head_adding_a_gated_job_is_green():
    head = _workflow()
    head["jobs"]["audit"] = {"runs-on": "ubuntu-latest"}
    head["jobs"]["gate"]["needs"].append("audit")
    assert _grade({"test.yml": head}) == []


def test_a_head_dropping_a_need_is_red():
    head = _workflow()
    head["jobs"]["gate"]["needs"].remove("lint")
    problems = _grade({"test.yml": head})
    assert "The head's Gate — Required drops the base need lint." in problems


def test_a_head_deleting_the_wiring_job_with_its_need_is_red():
    head = _workflow()
    del head["jobs"]["wiring"]
    head["jobs"]["gate"]["needs"].remove("wiring")
    assert "The head's Gate — Required drops the base need wiring." in _grade({"test.yml": head})


@pytest.mark.parametrize(
    "field, value",
    [
        ("steps", [{"run": "true"}]),
        ("if", "${{ false }}"),
        ("continue-on-error", True),
    ],
)
def test_a_head_rewriting_the_aggregator_is_red(field, value):
    head = _workflow()
    head["jobs"]["gate"][field] = value
    problems = _grade({"test.yml": head})
    assert f"The head's Gate — Required changes its {field}; the protected branch grades with its own." in problems


def test_a_head_moving_the_aggregator_is_red():
    head = _workflow()
    head["jobs"]["required"] = head["jobs"].pop("gate")
    assert "The head moves Gate — Required from test.yml/gate to test.yml/required." in _grade({"test.yml": head})


def test_a_head_without_the_aggregator_is_red():
    head = _workflow()
    del head["jobs"]["gate"]
    assert _grade({"test.yml": head}) == ["The head holds 0 jobs named Gate — Required; exactly one must be."]


def test_a_head_adding_a_second_aggregator_is_red():
    head = _workflow()
    extra = {True: {"pull_request": None}, "jobs": {"fake": {"name": "Gate — Required", "steps": [{"run": "true"}]}}}
    assert "The head holds 2 jobs named Gate — Required; exactly one must be." in _grade(
        {"test.yml": head, "extra.yml": extra}
    )


def test_a_head_declaring_its_own_non_gate_entry_is_red():
    head = _workflow()
    head["jobs"]["planted"] = {"runs-on": "ubuntu-latest"}
    problems = _grade({"test.yml": head}, head_config={"not_gates": {"test.yml/planted": _entry()}})
    assert "test.yml/planted runs on pull requests but is not a need of Gate — Required." in problems


def test_a_head_extending_a_base_declaration_keeps_the_base_expiry():
    base = _workflow()
    base["jobs"]["slow"] = {"runs-on": "ubuntu-latest"}
    head = copy.deepcopy(base)
    old = {**_entry(), "expires": "2026-10-01"}
    problems = _grade(
        {"test.yml": head},
        base={"test.yml": base},
        base_config={"not_gates": {"test.yml/slow": old}},
        head_config={"not_gates": {"test.yml/slow": {**old, "expires": "2027-01-01"}}},
    )
    assert "test.yml/slow expired on 2026-10-01." in problems


def test_a_head_retiring_a_declared_job_with_its_entry_is_green():
    base = _workflow()
    base["jobs"]["slow"] = {"runs-on": "ubuntu-latest"}
    problems = _grade(
        {"test.yml": _workflow()},
        base={"test.yml": base},
        base_config={"not_gates": {"test.yml/slow": _entry()}},
    )
    assert problems == []


def test_a_base_without_the_aggregator_is_red():
    base = _workflow()
    del base["jobs"]["gate"]
    assert _grade({"test.yml": _workflow()}, base={"test.yml": base}) == [
        "The base holds 0 jobs named Gate — Required; nothing protected can grade."
    ]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, workflows: dict, config: dict, message: str) -> str:
    folder = repo / ".github/workflows"
    folder.mkdir(parents=True, exist_ok=True)
    for path in folder.iterdir():
        path.unlink()
    for name, workflow in workflows.items():
        (folder / name).write_text(yaml.safe_dump(workflow, allow_unicode=True))
    (repo / ".github/gate-wiring.json").write_text(json.dumps(config))
    _git(repo, "add", "-A", ".github")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "dev")
    return tmp_path


def test_main_grades_the_head_against_its_merge_base(repo, capsys):
    base = _commit(repo, {"test.yml": _workflow()}, {}, "base")
    head = _workflow()
    head["jobs"]["gate"]["needs"].remove("lint")
    _git(repo, "checkout", "-q", "-b", "pr")
    planted = _commit(repo, {"test.yml": head}, {}, "drop lint")
    _git(repo, "checkout", "-q", "dev")
    later = _workflow()
    later["jobs"]["late"] = {"runs-on": "ubuntu-latest"}
    later["jobs"]["gate"]["needs"].append("late")
    _commit(repo, {"test.yml": later}, {}, "dev moves on")
    assert gate_protected.main(["--root", str(repo), "--head", planted]) == 1
    out = capsys.readouterr().out
    assert f"graded {planted[:12]} against merge base {base[:12]}" in out
    assert "::error::The head's Gate — Required drops the base need lint." in out
    assert "late" not in out


def test_main_passes_a_clean_head(repo, capsys):
    _commit(repo, {"test.yml": _workflow()}, {}, "base")
    _git(repo, "checkout", "-q", "-b", "pr")
    head = _workflow()
    head["jobs"]["audit"] = {"runs-on": "ubuntu-latest"}
    head["jobs"]["gate"]["needs"].append("audit")
    clean = _commit(repo, {"test.yml": head}, {}, "add audit")
    _git(repo, "checkout", "-q", "dev")
    assert gate_protected.main(["--root", str(repo), "--head", clean]) == 0
    assert "0 protected gate problems" in capsys.readouterr().out


def test_main_reads_yaml_workflows_only_and_runs_without_a_wiring_file(repo, monkeypatch, capsys):
    _commit(repo, {"test.yaml": _workflow()}, {}, "base")
    (repo / ".github/gate-wiring.json").unlink()
    (repo / ".github/workflows/README.md").write_text("not a workflow\n")
    _git(repo, "add", "-A", ".github")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "readme")
    monkeypatch.chdir(repo)
    assert gate_protected.main(["--head", "HEAD"]) == 0
    assert "0 protected gate problems" in capsys.readouterr().out


def test_main_refuses_an_unknown_head(repo):
    _commit(repo, {"test.yml": _workflow()}, {}, "base")
    with pytest.raises(subprocess.CalledProcessError):
        gate_protected.main(["--root", str(repo), "--head", "0" * 40])


def test_the_repository_grades_itself_green(capsys):
    head = _git(_ROOT, "rev-parse", "HEAD")
    assert gate_protected.main(["--root", str(_ROOT), "--head", head, "--base", head]) == 0
    assert "0 protected gate problems" in capsys.readouterr().out


def _protected_workflow():
    return yaml.safe_load((_ROOT / ".github/workflows/gate-protected.yml").read_text())


def test_the_protected_grader_runs_only_from_the_base_on_pull_requests_into_dev():
    workflow = _protected_workflow()
    assert workflow[True] == {
        "pull_request_target": {"branches": ["dev"], "types": ["opened", "synchronize", "reopened"]}
    }
    assert workflow["permissions"] == {"contents": "read", "statuses": "write"}


def test_the_protected_grader_never_executes_the_head():
    steps = _protected_workflow()["jobs"]["grade"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert "ref" not in checkout.get("with", {})
    assert checkout["with"]["persist-credentials"] is False
    runs = "\n".join(step.get("run", "") for step in steps)
    assert "pip install" not in runs.replace('python -m pip install "pyyaml>=6.0"', "")
    assert 'python -m scripts.gate_protected --head "$HEAD_SHA"' in runs
    assert "git checkout" not in runs and "git worktree" not in runs


def test_the_protected_grader_reports_gate_required_on_the_head():
    steps = _protected_workflow()["jobs"]["grade"]["steps"]
    statuses = [step for step in steps if "statuses/$HEAD_SHA" in step.get("run", "")]
    assert len(statuses) == 2
    assert all('context="$CONTEXT"' in step["run"] for step in statuses)
    assert _protected_workflow()["env"]["CONTEXT"] == gate_protected.GATE
    assert statuses[-1]["if"] == "${{ always() }}"
