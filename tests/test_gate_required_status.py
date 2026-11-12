import json
import subprocess
import urllib.parse
from pathlib import Path

import pytest
import yaml

from scripts import gate_required_status

ROOT = Path(__file__).resolve().parents[1]
HEAD = "957c261a915a038a247c18e710123872daf18603"


def _run(name: str, run_id: int, status: str = "completed", conclusion: str | None = "success") -> dict:
    return {"name": name, "id": run_id, "status": status, "conclusion": conclusion}


@pytest.mark.feature("gate-required-waits-for-aggregator")
@pytest.mark.parametrize(
    ("protected", "aggregator", "state"),
    [
        ("success", "pending", "pending"),
        ("success", "success", "success"),
        ("success", "failure", "failure"),
        ("failure", "pending", "failure"),
        ("failure", "success", "failure"),
        ("pending", "success", "pending"),
        ("pending", "pending", "pending"),
    ],
)
def test_gate_required_succeeds_only_when_both_halves_succeeded(protected, aggregator, state):
    assert gate_required_status.decide(protected, aggregator) == state


class FakeGitHub:
    def __init__(self, runs: list[dict]) -> None:
        self.runs = runs
        self.posted: list[list[str]] = []
        self.queries: list[str] = []

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        endpoint = argv[2]
        if "/check-runs?" in endpoint:
            self.queries.append(endpoint)
            wanted = urllib.parse.parse_qs(endpoint.split("?", 1)[1])["check_name"][0]
            body = {"check_runs": [run for run in self.runs if run["name"] == wanted]}
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")
        self.posted.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    def state(self) -> str:
        assert len(self.posted) == 1
        fields = dict(value.split("=", 1) for value in self.posted[0][4::2])
        assert self.posted[0][2] == f"repos/acme/repo/statuses/{HEAD}"
        assert fields["context"] == "Gate — Required"
        return fields["state"]


def _report(monkeypatch, runs: list[dict], *extra: str) -> FakeGitHub:
    github = FakeGitHub(runs)
    monkeypatch.setattr(gate_required_status.subprocess, "run", github)
    assert gate_required_status.main(["--repository", "acme/repo", "--head", HEAD, *extra]) == 0
    return github


@pytest.mark.feature("gate-required-waits-for-aggregator")
def test_a_passed_protected_grade_stays_pending_while_the_aggregator_has_no_check_run(monkeypatch):
    github = _report(monkeypatch, [], "--protected", "success")
    assert github.state() == "pending"
    assert all("app_id=15368" in query for query in github.queries)


@pytest.mark.feature("gate-required-waits-for-aggregator")
def test_a_queued_type_gate_holds_gate_required_pending(monkeypatch):
    runs = [_run("type-gate (3.12)", 5, "queued", None), _run("Gate — Protected", 2)]
    assert _report(monkeypatch, runs).state() == "pending"


@pytest.mark.feature("gate-required-waits-for-aggregator")
def test_the_aggregator_failing_on_the_type_gate_fails_gate_required(monkeypatch):
    runs = [_run("Gate — Protected", 2), _run("Gate — Required", 9, conclusion="failure")]
    assert _report(monkeypatch, runs).state() == "failure"


@pytest.mark.feature("gate-required-waits-for-aggregator")
def test_the_newest_aggregator_run_decides_after_a_rerun(monkeypatch):
    runs = [
        _run("Gate — Protected", 2),
        _run("Gate — Required", 9, conclusion="failure"),
        _run("Gate — Required", 12),
    ]
    assert _report(monkeypatch, runs).state() == "success"


@pytest.mark.feature("gate-required-waits-for-aggregator")
def test_a_green_aggregator_never_overrides_a_refused_protected_grade(monkeypatch):
    runs = [_run("Gate — Protected", 2, conclusion="failure"), _run("Gate — Required", 9)]
    assert _report(monkeypatch, runs).state() == "failure"


def test_an_explicit_protected_outcome_wins_over_its_running_check(monkeypatch):
    runs = [_run("Gate — Protected", 2, "in_progress", None), _run("Gate — Required", 9)]
    assert _report(monkeypatch, runs, "--protected", "success").state() == "success"


@pytest.mark.feature("gate-required-waits-for-aggregator")
def test_the_status_workflow_reports_after_every_pull_request_tests_run_from_trusted_code():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gate-required.yml").read_text())
    assert workflow[True] == {"workflow_run": {"workflows": ["Tests"], "types": ["completed"]}}
    job = workflow["jobs"]["report"]
    assert job["if"] == "github.event.workflow_run.event == 'pull_request'"
    assert job["permissions"] == {"checks": "read", "contents": "read", "statuses": "write"}
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"] == {"persist-credentials": False}
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    assert 'python -m scripts.gate_required_status --repository "$GITHUB_REPOSITORY" --head "$HEAD_SHA"' in runs
