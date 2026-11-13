import json
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import gate_required_status

ROOT = Path(__file__).resolve().parents[1]
HEAD = "957c261a915a038a247c18e710123872daf18603"


PULL_SUITE = 70


def _run(
    name: str, run_id: int, status: str = "completed", conclusion: str | None = "success", suite: int = PULL_SUITE
) -> dict:
    return {"name": name, "id": run_id, "status": status, "conclusion": conclusion, "check_suite": {"id": suite}}


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
def test_gate_required_succeeds_only_when_both_halves_succeeded(protected: str, aggregator: str, state: str) -> None:
    assert gate_required_status.decide(protected, aggregator) == state


READ = {"check": True, "capture_output": True, "text": True, "timeout": 30}
POST = {"check": True, "capture_output": True, "timeout": 30}
RUNS = f"repos/acme/repo/actions/workflows/test.yml/runs?head_sha={HEAD}&event=pull_request&per_page=1"
CHECKS = {
    "Gate — Required": f"repos/acme/repo/commits/{HEAD}/check-runs?check_name=Gate+%E2%80%94+Required&app_id=15368&per_page=100",
    "Gate — Protected": f"repos/acme/repo/commits/{HEAD}/check-runs?check_name=Gate+%E2%80%94+Protected&app_id=15368&per_page=100",
}


class FakeGitHub:
    def __init__(self, runs: list[dict], pull_runs: int = 1) -> None:
        self.runs = runs
        self.pull_runs = pull_runs
        self.posted: list[list[str]] = []
        self.queries: list[str] = []

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if argv[:2] == ["gh", "api"] and len(argv) == 3:
            assert kwargs == READ
            endpoint = argv[2]
            self.queries.append(endpoint)
            if endpoint == RUNS:
                body = {"workflow_runs": [{"id": 1, "check_suite_id": PULL_SUITE}] * self.pull_runs}
            else:
                name = next(name for name, query in CHECKS.items() if query == endpoint)
                body = {"check_runs": [run for run in self.runs if run["name"] == name]}
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")
        assert kwargs == POST
        self.posted.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    def state(self, target: str = "") -> str:
        assert len(self.posted) == 1
        state = self.posted[0][4].removeprefix("state=")
        assert self.posted[0] == [
            "gh",
            "api",
            f"repos/acme/repo/statuses/{HEAD}",
            "-f",
            f"state={state}",
            "-f",
            "context=Gate — Required",
            "-f",
            f"target_url={target}",
            "-f",
            f"description={gate_required_status.DESCRIPTIONS[state]}",
        ]
        return state


def _report(monkeypatch: pytest.MonkeyPatch, runs: list[dict], *extra: str, pull_runs: int = 1) -> FakeGitHub:
    github = FakeGitHub(runs, pull_runs)
    monkeypatch.setattr(gate_required_status.subprocess, "run", github)
    monkeypatch.setattr(gate_required_status.time, "sleep", lambda seconds: None)
    assert gate_required_status.main(["--repository", "acme/repo", "--head", HEAD, *extra]) == 0
    assert set(github.queries) <= {RUNS, *CHECKS.values()}
    return github


@pytest.mark.feature("gate-required-waits-for-aggregator")
def test_a_passed_protected_grade_stays_pending_while_the_aggregator_has_no_check_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github = _report(monkeypatch, [], "--protected", "success")
    assert github.state() == "pending"
    assert github.queries == [RUNS, CHECKS["Gate — Required"]]


@pytest.mark.feature("gate-required-waits-for-aggregator")
def test_a_queued_type_gate_holds_gate_required_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    runs = [_run("type-gate (3.12)", 5, "queued", None), _run("Gate — Protected", 2)]
    assert _report(monkeypatch, runs).state() == "pending"


@pytest.mark.feature("gate-required-waits-for-aggregator")
def test_the_aggregator_failing_on_the_type_gate_fails_gate_required(monkeypatch: pytest.MonkeyPatch) -> None:
    runs = [_run("Gate — Protected", 2), _run("Gate — Required", 9, conclusion="failure")]
    assert _report(monkeypatch, runs).state() == "failure"


@pytest.mark.feature("gate-required-waits-for-aggregator")
def test_the_newest_aggregator_run_decides_after_a_rerun(monkeypatch: pytest.MonkeyPatch) -> None:
    runs = [
        _run("Gate — Protected", 2),
        _run("Gate — Required", 9, conclusion="failure"),
        _run("Gate — Required", 12),
    ]
    assert _report(monkeypatch, runs).state() == "success"


@pytest.mark.feature("gate-required-waits-for-aggregator")
def test_a_green_aggregator_never_overrides_a_refused_protected_grade(monkeypatch: pytest.MonkeyPatch) -> None:
    runs = [_run("Gate — Protected", 2, conclusion="failure"), _run("Gate — Required", 9)]
    assert _report(monkeypatch, runs).state() == "failure"


@pytest.mark.feature("gate-required-waits-for-aggregator")
def test_an_aggregator_from_another_tests_run_on_the_head_never_decides(monkeypatch: pytest.MonkeyPatch) -> None:
    runs = [
        _run("Gate — Protected", 2),
        _run("Gate — Required", 9, conclusion="failure"),
        _run("Gate — Required", 15, suite=71),
    ]
    assert _report(monkeypatch, runs).state() == "failure"


@pytest.mark.feature("gate-required-waits-for-aggregator")
def test_no_pull_request_tests_run_holds_gate_required_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    runs = [_run("Gate — Protected", 2), _run("Gate — Required", 15, suite=71)]
    assert _report(monkeypatch, runs, pull_runs=0).state() == "pending"


@pytest.mark.feature("gate-required-waits-for-aggregator")
def test_the_status_workflow_waits_for_a_running_protected_grade(monkeypatch: pytest.MonkeyPatch) -> None:
    protected = _run("Gate — Protected", 2, "in_progress", None)
    reads = iter([None, None, "success"])

    def advance(seconds: float) -> None:
        conclusion = next(reads)
        if conclusion:
            protected.update(status="completed", conclusion=conclusion)

    github = FakeGitHub([protected, _run("Gate — Required", 9)])
    monkeypatch.setattr(gate_required_status.subprocess, "run", github)
    monkeypatch.setattr(gate_required_status.time, "sleep", advance)
    assert gate_required_status.main(["--repository", "acme/repo", "--head", HEAD]) == 0
    assert github.state() == "success"


def test_an_explicit_protected_outcome_wins_over_its_running_check(monkeypatch: pytest.MonkeyPatch) -> None:
    runs = [_run("Gate — Protected", 2, "in_progress", None), _run("Gate — Required", 9)]
    assert _report(monkeypatch, runs, "--protected", "success").state() == "success"


@pytest.mark.feature("gate-required-waits-for-aggregator")
def test_the_status_workflow_reports_after_every_pull_request_tests_run_from_trusted_code() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/gate-required.yml").read_text())
    assert workflow[True] == {"workflow_run": {"workflows": ["Tests"], "types": ["completed"]}}
    job = workflow["jobs"]["report"]
    assert job["if"] == "github.event.workflow_run.event == 'pull_request'"
    assert job["permissions"] == {"actions": "read", "checks": "read", "contents": "read", "statuses": "write"}
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"] == {"persist-credentials": False}
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    assert 'python -m scripts.gate_required_status --repository "$GITHUB_REPOSITORY" --head "$HEAD_SHA"' in runs


@pytest.mark.feature("gate-required-waits-for-aggregator")
def test_the_announced_protected_workflow_reports_through_the_resolver() -> None:
    accepted = json.loads((ROOT / "tests/registry/FROZEN_GATES.json").read_text())[
        ".github/workflows/gate-protected.yml"
    ]
    candidate = yaml.safe_load((ROOT / "tests/registry/FROZEN_CANDIDATES" / accepted[-1]).read_text())
    steps = candidate["jobs"]["grade"]["steps"]
    assert not [
        step for step in steps if "statuses/$HEAD_SHA" in step.get("run", "") and "state=pending" not in step["run"]
    ]
    report = steps[-1]
    assert report["if"] == "${{ always() }}"
    assert "python -m scripts.gate_required_status" in report["run"]
    assert '--protected "$protected"' in report["run"]
    assert candidate["permissions"]["checks"] == "read"


@pytest.mark.parametrize(
    "argv",
    [
        ["--head", HEAD],
        ["--repository", "acme/repo"],
        ["--repository", "acme/repo", "--head", HEAD, "--protected", "pending"],
    ],
)
def test_the_command_line_refuses_a_missing_head_or_repository_and_an_unknown_outcome(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        gate_required_status.main(argv)


def test_the_report_posts_the_target_url_and_prints_the_decision(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = [_run("Gate — Protected", 2, conclusion="failure"), _run("Gate — Required", 9)]
    github = _report(monkeypatch, runs, "--protected", "failure", "--target-url", "https://run")
    assert github.state("https://run") == "failure"
    assert capsys.readouterr().out == "Gate Required on 957c261a915a: failure (protected failure).\n"


def test_a_protected_grade_still_running_after_every_poll_reads_once_more_and_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github = FakeGitHub([_run("Gate — Protected", 2, "in_progress", None), _run("Gate — Required", 9)])
    sleeps: list[float] = []
    monkeypatch.setattr(gate_required_status.subprocess, "run", github)
    monkeypatch.setattr(gate_required_status.time, "sleep", sleeps.append)
    assert gate_required_status.main(["--repository", "acme/repo", "--head", HEAD]) == 0
    assert github.state() == "pending"
    assert sleeps == [10] * 19
    assert github.queries.count(CHECKS["Gate — Protected"]) == 20


def test_the_last_poll_result_decides_when_the_protected_grade_finishes_at_the_final_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = _run("Gate — Protected", 2, "in_progress", None)
    sleeps: list[float] = []

    def advance(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 19:
            protected.update(status="completed", conclusion="success")

    github = FakeGitHub([protected, _run("Gate — Required", 9)])
    monkeypatch.setattr(gate_required_status.subprocess, "run", github)
    monkeypatch.setattr(gate_required_status.time, "sleep", advance)
    assert gate_required_status.main(["--repository", "acme/repo", "--head", HEAD]) == 0
    assert github.state() == "success"
    assert len(sleeps) == 19
