import json
from pathlib import Path

import pytest
import yaml

from scripts import ci_unit_rerun

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
PLAN = [{"python-version": "3.12", "shard": shard, "total": 14, "predicted_seconds": 200.0} for shard in range(1, 15)]


def _job(name, conclusion, job_id=0):
    return {"name": name, "conclusion": conclusion, "id": job_id}


def _jobs():
    return yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]


def test_each_lost_shard_is_rerun_once_with_its_queue_plan_leg():
    jobs = [
        _job("lint", "success"),
        _job("unit (3.12, 3)", "failure"),
        _job("unit (3.12, 4)", "success"),
        _job("unit (3.12, 14)", "cancelled"),
    ]
    matrix, reason = ci_unit_rerun.decide(jobs, PLAN, lambda job: True)
    assert matrix == [PLAN[2], PLAN[13]]
    assert reason == "rerunning 2 lost unit shards once"


def test_a_shard_that_failed_its_tests_reruns_nothing():
    jobs = [
        _job("unit (3.12, 3)", "failure", 3),
        _job("unit (3.12, 5)", "failure", 5),
        _job("unit (3.12, 6)", "failure"),
    ]
    matrix, reason = ci_unit_rerun.decide(jobs, PLAN, lambda job: job["id"] == 3)
    assert matrix == []
    assert reason == "failed without a runner loss signature: unit (3.12, 5), unit (3.12, 6)"


@pytest.mark.parametrize("missing", ["--jobs", "--repo", "--plan"])
def test_every_argument_is_required(tmp_path, missing):
    values = {"--jobs": str(tmp_path / "jobs.jsonl"), "--repo": "o/r", "--plan": ""}
    argv = [word for flag, value in values.items() if flag != missing for word in (flag, value)]
    with pytest.raises(SystemExit) as raised:
        ci_unit_rerun.main(argv)
    assert raised.value.code == 2


@pytest.mark.parametrize(("conclusion", "lost"), [("timed_out", True), ("failure", False)])
def test_a_timed_out_or_unsigned_shard_reruns_nothing(conclusion, lost):
    matrix, _ = ci_unit_rerun.decide([_job("unit (3.12, 2)", conclusion)], PLAN, lambda job: lost)
    assert matrix == []


def test_other_jobs_and_passing_shards_are_not_classified():
    asked = []
    jobs = [_job("lint", "failure"), _job("unit rerun (3.12, 2)", "failure"), _job("unit (3.12, 2)", "success")]
    matrix, reason = ci_unit_rerun.decide(jobs, PLAN, lambda job: asked.append(job) or True)
    assert (matrix, reason, asked) == ([], "no unit shard lost its runner", [])


def test_a_lost_leg_missing_from_the_plan_reruns_nothing():
    matrix, reason = ci_unit_rerun.decide([_job("unit (3.12, 20)", "failure")], PLAN, lambda job: True)
    assert matrix == []
    assert reason == "the queue plan lacks a lost leg: [('3.12', 20)]"


@pytest.mark.parametrize(("lost", "code"), [(True, 0), (False, 1)])
def test_main_writes_the_matrix_and_fails_without_a_rerun(monkeypatch, tmp_path, capsys, lost, code):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    listing = tmp_path / "jobs.jsonl"
    listing.write_text(
        json.dumps(_job("unit (3.12, 2)", "failure", 9)) + "\n\n" + json.dumps(_job("lint", "success")) + "\n"
    )
    monkeypatch.setattr(
        ci_unit_rerun,
        "logged_loss",
        lambda repo, job, seen: seen.setdefault(str(job["id"]), (repo, job["id"]) == ("o/r", 9) and lost),
    )
    plan = json.dumps(PLAN[:3])[1:-1]
    argv = ["--jobs", str(listing), "--repo", "o/r", "--plan", plan]
    assert ci_unit_rerun.main(argv) == code
    expected = [PLAN[1]] if lost else []
    assert output.read_text() == f"matrix={json.dumps(expected)}\n"
    reason = "rerunning 1 lost unit shards once" if lost else "failed without a runner loss signature: unit (3.12, 2)"
    assert json.loads(capsys.readouterr().out) == {"rerun": expected, "reason": reason}


@pytest.mark.parametrize(
    ("log", "expected"),
    [
        ("##[error]The action 'Run tests' has timed out after 12 minutes.", []),
        (
            "##[error]The action 'Run tests' has timed out after 12 minutes.\n"
            "##[error]The runner has received a shutdown signal.",
            [],
        ),
        ("##[error]The runner has received a shutdown signal.", [PLAN[1]]),
        ("", [PLAN[1]]),
    ],
    ids=["timeout", "timeout-then-shutdown", "shutdown", "empty-log"],
)
@pytest.mark.parametrize("unreadable", [False, True])
def test_recovery_uses_step_results_to_distinguish_timeout_from_runner_loss(
    monkeypatch, tmp_path, capsys, log, expected, unreadable
):
    import subprocess

    from scripts import ci_queue_retry

    def api(path, *flags):
        if path.endswith("/annotations"):
            return json.dumps([{"message": log}] if log else [])
        if unreadable:
            raise subprocess.CalledProcessError(1, ["gh", "api", path])
        return log

    monkeypatch.setattr(ci_queue_retry, "_api", api)
    listing = tmp_path / "jobs.jsonl"
    listing.write_text(json.dumps(_job("unit (3.12, 2)", "failure", 9)))
    plan = json.dumps(PLAN[:3])[1:-1]
    assert ci_unit_rerun.main(["--jobs", str(listing), "--repo", "o/r", "--plan", plan]) == (0 if expected else 1)
    assert json.loads(capsys.readouterr().out)["rerun"] == expected


def test_the_rerun_job_runs_the_unit_shard_steps_for_the_classified_legs():
    jobs = _jobs()
    lost, rerun = jobs["unit-lost"], jobs["unit-rerun"]
    assert lost["if"] == (
        "${{ !cancelled() && github.event_name == 'merge_group'"
        " && (needs.unit.result == 'failure' || needs.unit.result == 'cancelled') }}"
    )
    assert lost["runs-on"] == jobs["shard-check"]["runs-on"]
    classify = lost["steps"][-1]
    assert classify["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert "scripts.ci_budget.jobs" in classify["run"]
    assert "scripts.ci_unit_rerun --jobs" in classify["run"]
    assert "permissions" not in lost
    assert rerun["name"] == "unit rerun (${{ matrix.python-version }}, ${{ matrix.shard }})"
    assert rerun["if"] == "${{ !cancelled() && needs.unit-lost.result == 'success' }}"
    assert rerun["strategy"]["matrix"] == {"include": "${{ fromJSON(needs.unit-lost.outputs.matrix) }}"}
    for key in ("steps", "env", "runs-on", "timeout-minutes", "permissions"):
        assert rerun[key] == jobs["unit"][key], key


def test_judges_wait_for_the_rerun_and_the_gate_reads_both_jobs():
    jobs = _jobs()
    if "unit-rerun" not in jobs["shard-check"]["needs"]:
        assert jobs["shard-check"]["needs"] == ["split", "select", "reuse", "admission"]
        assert any("python -m scripts.ci_gate_shards" in step.get("run", "") for step in jobs["shard-check"]["steps"])
    assert {"unit-lost", "unit-rerun"} <= set(jobs["stage-budget"]["needs"])
    assert {"unit-lost", "unit-rerun"} <= set(jobs["gate-required"]["needs"])
