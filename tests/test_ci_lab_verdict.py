"""Tests for the lab-verdict job: base controlled authority, the lab off switch and the lab App verdict poll."""

import json
import runpy
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import ci_lab_verdict as verdict
from scripts import ci_reuse

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = {"on": "push", "jobs": {"lint": {}, "type-gate": {}, "unit": {}}}
LAB = "agenticore-app"


def tree(root: Path, authoritative: list[str], prefix: str = "lab", queue: list[str] | None = None) -> Path:
    """Write a repository tree holding one workflow and a delivery.yaml naming the authoritative gates, and the
    queue gates when given."""
    (root / ".github/workflows").mkdir(parents=True)
    (root / ".github/workflows/test.yml").write_text(yaml.safe_dump(WORKFLOW))
    lab: dict[str, Any] = {
        "authoritative": authoritative,
        "mutation": {"run_on": "every_push", "mutants_per_changed_line": "all", "arid": []},
        "check_prefix": prefix,
    }
    if queue is not None:
        lab["queue"] = {"authoritative": queue}
    (root / "delivery.yaml").write_text(yaml.safe_dump({"lab": lab}))
    return root


def run(run_id: int, status: str, result: str | None = None, app: str = LAB) -> dict[str, Any]:
    """Return one check run as the GitHub API lists it."""
    return {"id": run_id, "status": status, "conclusion": result, "app": {"slug": app}}


class Clock:
    """A fake monotonic clock that advances only when the poll sleeps."""

    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def scripted(answers: dict[str, list[list[dict[str, Any]]]]) -> tuple[Callable[[str], list[dict[str, Any]]], list[str]]:
    """Return a fetch that answers each check name with its next scripted listing, and the names it was asked."""
    asked: list[str] = []

    def fetch(check: str) -> list[dict[str, Any]]:
        asked.append(check)
        listings = answers[check]
        return listings.pop(0) if len(listings) > 1 else listings[0]

    return fetch, asked


def test_head_cannot_widen_authority(tmp_path: Path) -> None:
    grader = tree(tmp_path / "grader", [])
    tree(tmp_path / "head", ["lint", "unit"])
    assert verdict.authority(grader, "pull_request", {}) == {"authoritative": "{}"}


def test_authority_publishes_the_base_gates_as_an_object(tmp_path: Path) -> None:
    grader = tree(tmp_path, ["unit", "lint"])
    assert verdict.authority(grader, "pull_request", {}) == {"authoritative": '{"lint": true, "unit": true}'}


def test_lab_off_keeps_every_actions_gate(tmp_path: Path) -> None:
    grader = tree(tmp_path, ["lint"])
    assert verdict.authority(grader, "pull_request", {"AGENTIHOOKS_LAB_OFF": "true"}) == {"authoritative": "{}"}


@pytest.mark.parametrize("value", ["", "false", "1", "TRUE"])
def test_only_true_switches_the_lab_off(tmp_path: Path, value: str) -> None:
    grader = tree(tmp_path, ["lint"])
    assert verdict.authority(grader, "pull_request", {"AGENTIHOOKS_LAB_OFF": value}) == {
        "authoritative": '{"lint": true}'
    }


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "pull_request_target", ""])
def test_authority_is_published_only_on_pull_requests_and_merge_groups(tmp_path: Path, event: str) -> None:
    assert verdict.authority(tree(tmp_path, ["lint"], queue=["unit"]), event, {}) == {}


def test_merge_group_authority_publishes_the_base_queue_gates(tmp_path: Path) -> None:
    grader = tree(tmp_path, ["lint"], queue=["unit", "type-gate"])
    assert verdict.authority(grader, "merge_group", {}) == {"authoritative": '{"type-gate": true, "unit": true}'}
    assert verdict.authority(grader, "pull_request", {}) == {"authoritative": '{"lint": true}'}


def test_merge_group_authority_is_empty_without_queue_gates(tmp_path: Path) -> None:
    assert verdict.authority(tree(tmp_path, ["lint"]), "merge_group", {}) == {"authoritative": "{}"}


def test_head_cannot_widen_queue_authority(tmp_path: Path) -> None:
    grader = tree(tmp_path / "grader", [], queue=[])
    tree(tmp_path / "head", [], queue=["unit"])
    assert verdict.authority(grader, "merge_group", {}) == {"authoritative": "{}"}


def test_lab_off_keeps_every_actions_queue_gate(tmp_path: Path) -> None:
    grader = tree(tmp_path, [], queue=["unit"])
    assert verdict.authority(grader, "merge_group", {"AGENTIHOOKS_LAB_OFF": "true"}) == {"authoritative": "{}"}


def test_absent_runs_have_no_verdict() -> None:
    assert verdict.conclusion([], LAB) is None


def test_pending_run_has_no_verdict() -> None:
    assert verdict.conclusion([run(1, "in_progress")], LAB) is None


@pytest.mark.parametrize(
    ("result", "expected"),
    [("success", "success"), ("skipped", "skipped"), ("failure", "failure"), ("neutral", "failure"), (None, "failure")],
)
def test_only_success_and_skipped_pass_through(result: str | None, expected: str) -> None:
    assert verdict.conclusion([run(1, "completed", result)], LAB) == expected


def test_newest_trusted_run_decides() -> None:
    runs = [run(7, "completed", "failure"), run(9, "completed", "success"), run(8, "completed", "failure")]
    assert verdict.conclusion(runs, LAB) == "success"
    assert verdict.conclusion([run(9, "in_progress"), run(8, "completed", "success")], LAB) is None


def test_named_app_is_the_only_trusted_one() -> None:
    runs = [run(5, "completed", "failure", LAB), run(6, "completed", "success", "other-app")]
    assert verdict.conclusion(runs, LAB) == "failure"
    assert verdict.conclusion(runs, "other-app") == "success"
    assert verdict.conclusion(runs, "") is None


@pytest.mark.feature("lab-lost-unit-leaves-gate-neutral")
def test_a_check_whose_newest_trusted_run_completed_neutral_is_named_as_lost() -> None:
    listings = {
        "lab/lint": [run(1, "completed", "failure"), run(2, "completed", "neutral")],
        "lab/type-gate": [run(3, "completed", "success")],
        "lab/test-count": [run(4, "completed", "neutral"), run(5, "in_progress")],
        "lab/docs": [run(6, "completed", "neutral", app="another-app")],
        "lab/semgrep": [],
    }
    assert verdict.lost(listings, LAB) == ["lab/lint"]


@pytest.mark.feature("lab-lost-unit-leaves-gate-neutral")
def test_main_verdict_fails_a_gate_the_lab_lost_a_unit_of_and_names_the_rerequest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    grader = tree(tmp_path / "grader", ["lint", "type-gate"])
    output = tmp_path / "output"
    listings = {"lab/lint": [run(1, "completed", "neutral")], "lab/type-gate": [run(2, "completed", "success")]}

    def fetcher(repo: str, sha: str) -> Callable[[str], list[dict[str, Any]]]:
        return lambda check: listings[check]

    monkeypatch.setattr(verdict, "fetcher", fetcher)
    args = ["verdict", str(grader), '{"lint": true, "type-gate": true}', "org/repo", "abc", LAB]

    assert verdict.main(args, {"GITHUB_OUTPUT": str(output)}) == 0

    assert output.read_text() == "lab_lint=failure\nlab_type-gate=success\n"
    assert capsys.readouterr().out.splitlines()[-1] == (
        "::error::The lab lost a unit of lab/lint and posted no verdict: "
        "rerequest the lab/lint check to grade it again."
    )


def test_pending_then_success_polls_until_complete() -> None:
    clock = Clock()
    fetch, asked = scripted(
        {"lab/lint": [[run(1, "queued")], [run(1, "in_progress")], [run(1, "completed", "success")]]}
    )
    assert verdict.verdicts({"lint": "lab/lint"}, fetch, LAB, 60, clock, clock.sleep) == {"lint": "success"}
    assert asked == ["lab/lint"] * 3
    assert clock.slept == [15.0, 15.0]


def test_failure_is_published() -> None:
    clock = Clock()
    fetch, _ = scripted({"lab/lint": [[run(1, "completed", "failure")]]})
    assert verdict.verdicts({"lint": "lab/lint"}, fetch, LAB, 60, clock, clock.sleep) == {"lint": "failure"}
    assert clock.slept == []


def test_runs_from_other_apps_never_satisfy_the_poll() -> None:
    clock = Clock()
    fetch, asked = scripted({"lab/lint": [[run(1, "completed", "success", "other-app")]]})
    assert verdict.verdicts({"lint": "lab/lint"}, fetch, LAB, 15, clock, clock.sleep) == {"lint": "failure"}
    assert len(asked) == 2


@pytest.mark.parametrize("extra", [[], ["{}", "org/repo", "abc", LAB]])
def test_unknown_step_is_refused(tmp_path: Path, extra: list[str]) -> None:
    args = ["grade", str(tree(tmp_path, ["lint"])), "pull_request", *extra]
    with pytest.raises(ValueError, match="^unknown step 'grade'; expected authority or verdict$"):
        verdict.main(args, {"GITHUB_OUTPUT": str(tmp_path / "out")})


def test_verdict_refuses_an_unnamed_app_before_polling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verdict, "fetcher", lambda repo, sha: pytest.fail("polled"))
    args = ["verdict", str(tree(tmp_path, ["lint"])), '{"lint": true}', "org/repo", "abc", ""]
    with pytest.raises(ValueError, match="^no lab App is named; set AGENTIHOOKS_LAB_APP to the lab App slug$"):
        verdict.main(args, {"GITHUB_OUTPUT": str(tmp_path / "out")})
    assert not (tmp_path / "out").exists()


def test_absent_verdict_fails_at_the_deadline() -> None:
    clock = Clock()
    fetch, asked = scripted({"lab/lint": [[]]})
    assert verdict.verdicts({"lint": "lab/lint"}, fetch, LAB, 30, clock, clock.sleep) == {"lint": "failure"}
    assert clock.slept == [15.0, 15.0]
    assert len(asked) == 3


def test_pending_verdict_fails_at_the_deadline() -> None:
    clock = Clock()
    fetch, _ = scripted({"lab/lint": [[run(1, "in_progress")]]})
    assert verdict.verdicts({"lint": "lab/lint"}, fetch, LAB, 20, clock, clock.sleep) == {"lint": "failure"}
    assert clock.slept == [15.0, 15.0]


def test_completed_gates_are_not_asked_again() -> None:
    clock = Clock()
    fetch, asked = scripted(
        {"lab/lint": [[run(1, "completed", "success")]], "lab/unit": [[], [run(2, "completed", "failure")]]}
    )
    checks = {"lint": "lab/lint", "unit": "lab/unit"}
    assert verdict.verdicts(checks, fetch, LAB, 60, clock, clock.sleep) == {"lint": "success", "unit": "failure"}
    assert asked == ["lab/lint", "lab/unit", "lab/unit"]


def test_no_gates_needs_no_poll() -> None:
    clock = Clock()
    fetch, asked = scripted({})
    assert verdict.verdicts({}, fetch, LAB, 60, clock, clock.sleep) == {}
    assert asked == []
    assert clock.slept == []


def test_fetch_reads_latest_runs_through_the_rate_limited_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], float, bool]] = []

    def gh_api(args: list[str], *, timeout: float, text: bool = False) -> subprocess.CompletedProcess:
        calls.append((args, timeout, text))
        return subprocess.CompletedProcess(args, 0, json.dumps({"total_count": 1, "check_runs": [run(3, "queued")]}))

    monkeypatch.setattr(verdict, "gh_api", gh_api)
    assert verdict.fetcher("org/repo", "abc")("lab/type gate&x") == [run(3, "queued")]
    assert calls == [
        (
            ["repos/org/repo/commits/abc/check-runs?check_name=lab/type%20gate%26x&filter=latest&per_page=100"],
            30.0,
            True,
        )
    ]


def test_lab_outputs_publish_every_authoritative_gate(tmp_path: Path) -> None:
    grader = tree(tmp_path, ["lint"], prefix="grade")
    fetch, asked = scripted(
        {
            "grade/lint": [[run(1, "completed", "success")]],
            "grade/unit": [[run(3, "completed", "cancelled")]],
            "grade/type-gate": [[run(2, "completed", "skipped")]],
        }
    )
    authoritative = '{"unit": true, "lint": true, "type-gate": true, "docs": false}'
    outputs = verdict.lab_outputs(grader, authoritative, fetch, LAB, 0)
    assert outputs == {"lab_lint": "success", "lab_type-gate": "skipped", "lab_unit": "failure"}
    assert asked == ["grade/lint", "grade/type-gate", "grade/unit"]


@pytest.mark.parametrize("authoritative", ["", "{}"])
def test_lab_outputs_publish_nothing_without_authority(tmp_path: Path, authoritative: str) -> None:
    fetch, asked = scripted({})
    assert verdict.lab_outputs(tree(tmp_path, []), authoritative, fetch, LAB, 60) == {}
    assert asked == []


def test_main_authority_writes_the_step_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    grader = tree(tmp_path / "grader", ["lint"])
    output = tmp_path / "output"
    output.write_text("before=1\n")
    environ = {"GITHUB_OUTPUT": str(output)}
    args = ["authority", str(grader), "pull_request"]
    assert verdict.main(args, environ) == 0
    assert output.read_text() == 'before=1\nauthoritative={"lint": true}\n'
    assert capsys.readouterr().out == 'authoritative={"lint": true}\n'


def test_main_authority_honours_lab_off(tmp_path: Path) -> None:
    output = tmp_path / "output"
    environ = {"GITHUB_OUTPUT": str(output), "AGENTIHOOKS_LAB_OFF": "true"}
    args = ["authority", str(tree(tmp_path / "grader", ["lint"])), "pull_request"]
    assert verdict.main(args, environ) == 0
    assert output.read_text() == "authoritative={}\n"


def test_publish_writes_one_line_per_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "output"
    verdict.publish({"lab_lint": "success", "lab_unit": "failure"}, {"GITHUB_OUTPUT": str(output)})
    assert output.read_text() == "lab_lint=success\nlab_unit=failure\n"
    assert capsys.readouterr().out == "lab_lint=success\nlab_unit=failure\n"


def test_main_verdict_polls_the_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    grader = tree(tmp_path / "grader", ["lint"])
    output = tmp_path / "output"
    seen: list[tuple[str, str]] = []
    waits: list[tuple[str, float]] = []

    def fetcher(repo: str, sha: str) -> Callable[[str], list[dict[str, Any]]]:
        seen.append((repo, sha))
        return lambda check: [run(1, "completed", "failure")]

    real = verdict.verdicts

    def recorded(checks: dict[str, str], fetch: Any, app: str, wait: float) -> dict[str, str]:
        waits.append((app, wait))
        clock = Clock()
        return real(checks, fetch, app, wait, clock, clock.sleep)

    monkeypatch.setattr(verdict, "fetcher", fetcher)
    monkeypatch.setattr(verdict, "verdicts", recorded)
    args = ["verdict", str(grader), '{"lint": true}', "org/repo", "abc", LAB]
    assert verdict.main(args, {"GITHUB_OUTPUT": str(output)}) == 0
    assert output.read_text() == "lab_lint=failure\n"
    assert seen == [("org/repo", "abc")]
    assert waits == [(LAB, 1200.0)]


def test_module_entry_exits_with_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "output"
    grader = tree(tmp_path / "grader", ["lint"])
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.delenv("AGENTIHOOKS_LAB_OFF", raising=False)
    monkeypatch.setattr(sys, "argv", ["scripts.ci_lab_verdict", "authority", str(grader), "push"])
    monkeypatch.delitem(sys.modules, "scripts.ci_lab_verdict", raising=False)
    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("scripts.ci_lab_verdict", run_name="__main__")
    assert stopped.value.code == 0
    assert output.read_text() == ""


def jobs() -> dict[str, Any]:
    """Return the Tests workflow jobs."""
    return yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]


def test_the_job_reads_authority_only_from_the_protected_base() -> None:
    job = jobs()["lab-verdict"]
    assert job["if"] == "github.event_name == 'pull_request'"
    assert "needs" not in job
    assert job["permissions"] == {"contents": "read"}
    [checkout, *_] = job["steps"]
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
    assert checkout["with"]["persist-credentials"] is False
    graded = [step for step in job["steps"] if step.get("id") in ("authority", "verdict")]
    assert [step["id"] for step in graded] == ["authority", "verdict"]
    assert all(step["working-directory"] == "${{ github.workspace }}/" + checkout["with"]["path"] for step in graded)
    assert graded[0]["env"] == {"AGENTIHOOKS_LAB_OFF": "${{ vars.AGENTIHOOKS_LAB_OFF }}"}
    assert "ci_lab_verdict authority . pull_request\n" in graded[0]["run"]
    assert graded[1]["run"].endswith('verdict . "$AUTHORITATIVE" "$GITHUB_REPOSITORY" "$HEAD" "$LAB_APP"')
    assert graded[1]["env"]["HEAD"] == "${{ github.event.pull_request.head.sha }}"
    token = next(step for step in job["steps"] if step.get("id") == "app-token")
    assert {key for key in token["with"] if key.startswith("permission-")} == {"permission-checks"}
    assert token["with"]["permission-checks"] == "read"
    assert graded[1]["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert token["if"] == graded[1]["if"] == "steps.authority.outputs.authoritative != '{}'"


def test_the_job_publishes_a_result_for_every_declared_lab_gate() -> None:
    outputs = dict(jobs()["lab-verdict"]["outputs"])
    declared = json.loads((ROOT / ".github/gate-wiring.json").read_text())["lab_gates"]
    gates = {entry.removeprefix("lab/") for entry in declared}
    assert outputs.pop("authoritative") == "${{ steps.authority.outputs.authoritative }}"
    assert outputs == {f"lab_{gate}": f"${{{{ steps.verdict.outputs['lab_{gate}'] }}}}" for gate in gates}


def test_gate_required_grades_the_lab_verdict() -> None:
    gate = jobs()["gate-required"]
    assert "lab-verdict" in gate["needs"]
    assert ci_reuse.LAB_VERDICT in gate["steps"][0]["run"]
