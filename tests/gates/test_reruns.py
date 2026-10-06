import json
import subprocess

import pytest

from scripts.gates import Call, Gate, Who
from scripts.gates.budget import Budget
from scripts.gates.reruns import RerunBudget, Target, exempt, gh_jobs, refusal, targets
from scripts.gates.verdicts import Verdicts

ME = Who(name="engineer@1-1", swarm="demo", lane="eng", task="t1")
HEAD = "a" * 40
FAILED = [{"head_sha": HEAD, "conclusion": "failure", "runner_name": "GitHub Actions 3"}]
NO_RUNNER = [
    {"head_sha": HEAD, "conclusion": "success", "runner_name": "GitHub Actions 1"},
    {"head_sha": HEAD, "conclusion": "cancelled", "runner_name": ""},
    {"head_sha": HEAD, "conclusion": "cancelled", "runner_name": None},
]


def bash(command):
    return Call("Bash", {"command": command})


def state(tmp_path):
    return Verdicts("demo", "reruns", tmp_path)


def rows(tmp_path):
    path = tmp_path / "demo" / "gates" / "log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def gate_over(found, seen=None):
    def jobs(target):
        if seen is not None:
            seen.append(target)
        return found[target.run or target.job]

    return RerunBudget(jobs=jobs)


def test_it_is_a_gate_that_ships_enforcing_with_two_reruns():
    gate = RerunBudget()
    assert isinstance(gate, Gate)
    assert (gate.name, gate.default_mode, gate.cap) == ("reruns", "enforce", 2)


def test_it_matches_bash_calls_naming_gh_and_rerun():
    gate = RerunBudget()
    assert gate.matches(bash("gh run rerun 1 --failed"))
    assert not gate.matches(bash("gh run view 1"))
    assert not gate.matches(bash("echo rerun"))
    assert not gate.matches(Call("Read", {"command": "gh run rerun 1"}))


def test_targets_read_the_run_the_job_and_the_repo_of_every_rerun():
    assert list(targets("cd x && gh run rerun 11 --failed; /usr/bin/gh run rerun --job=22 -R o/r")) == [
        Target(run="11"),
        Target(job="22", repo="o/r"),
    ]
    assert list(targets("gh run rerun -j 33 --repo o/r --debug")) == [Target(job="33", repo="o/r")]
    assert list(targets("gh run rerun --job")) == [Target()]
    assert list(targets("gh run view 1; gh pr checks 2; echo gh run rerun")) == []


def test_a_third_rerun_on_one_head_is_refused_and_the_second_allowed(tmp_path):
    gate = gate_over({"1": FAILED, "2": FAILED, "3": FAILED})
    decisions = [gate.decide(bash(f"gh run rerun {run} --failed"), ME, state(tmp_path)) for run in "123"]
    assert [d.allowed for d in decisions] == [True, True, False]
    assert decisions[2].reason == refusal("demo", HEAD, 2)


def test_a_rerun_of_jobs_cancelled_without_a_runner_is_allowed_and_not_counted(tmp_path):
    gate = gate_over({"1": FAILED, "2": FAILED, "9": NO_RUNNER})
    for run in "129":
        assert gate.decide(bash(f"gh run rerun {run}"), ME, state(tmp_path)).allowed
    assert len(rows(tmp_path)) == 2


def test_every_counted_rerun_writes_a_count_row(tmp_path):
    gate = gate_over({"1": FAILED})
    gate.decide(bash("gh run rerun 1"), ME, state(tmp_path))
    assert [(r["gate"], r["kind"], r["agent"], r["task"], r["tool"], r["reason"]) for r in rows(tmp_path)] == [
        ("reruns", "count", ME.name, "t1", "Bash", f"CI reruns 1 of 2 on head {HEAD[:12]}")
    ]
    assert Budget("demo", "reruns", tmp_path).spent(HEAD, "reruns") == 1


def test_an_exempt_rerun_does_not_stop_the_next_one_in_the_command_from_counting(tmp_path):
    gate = gate_over({"1": FAILED, "9": NO_RUNNER})
    assert gate.decide(bash("gh run rerun 9 && gh run rerun 1"), ME, state(tmp_path)).allowed
    assert len(rows(tmp_path)) == 1


def test_each_head_has_its_own_budget(tmp_path):
    other = [{**FAILED[0], "head_sha": "b" * 40}]
    gate = gate_over({"1": FAILED, "2": FAILED, "3": other})
    for run in "12":
        gate.decide(bash(f"gh run rerun {run}"), ME, state(tmp_path))
    assert gate.decide(bash("gh run rerun 3"), ME, state(tmp_path)).allowed


def test_two_reruns_in_one_command_both_count(tmp_path):
    gate = gate_over({"1": FAILED, "2": FAILED, "3": FAILED})
    assert gate.decide(bash("gh run rerun 1 && gh run rerun 2"), ME, state(tmp_path)).allowed
    assert not gate.decide(bash("gh run rerun 3"), ME, state(tmp_path)).allowed


def test_a_rerun_naming_no_run_or_no_jobs_is_let_through_unread(tmp_path):
    seen = []
    gate = gate_over({"1": []}, seen)
    assert gate.decide(bash("gh run rerun"), ME, state(tmp_path)).allowed
    assert gate.decide(bash("gh run rerun 1"), ME, state(tmp_path)).allowed
    assert seen == [Target(run="1")]
    assert rows(tmp_path) == []


def test_a_session_outside_a_swarm_is_never_read_or_counted(tmp_path):
    gate = RerunBudget(cap=0, jobs=lambda target: pytest.fail("read outside a swarm"))
    assert gate.decide(bash("gh run rerun 1"), Who(), state(tmp_path)).allowed


def test_exempt_only_when_every_unsuccessful_job_was_cancelled_without_a_runner():
    ran = {"conclusion": "cancelled", "runner_name": "GitHub Actions 2"}
    passed = [{"conclusion": c, "runner_name": "r"} for c in ("success", "skipped", "neutral")]
    assert exempt(NO_RUNNER)
    assert not exempt([*NO_RUNNER, ran])
    assert not exempt(FAILED)
    assert not exempt([{"conclusion": None, "runner_name": ""}])
    assert not exempt(passed)
    assert not exempt([])


def test_the_refusal_names_the_spend_the_cap_and_the_way_out():
    assert refusal("demo", HEAD, 2) == (
        f"CI reruns on pull request head {HEAD[:12]} are spent: 2 of 2. Read the failed job's log and push a fix, "
        'or block with agentihooks swarm demo block "<why>"'
    )


def fake_gh(answers):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(answers[argv[2]]), stderr="")

    run.calls = calls
    return run


def test_gh_jobs_reads_a_runs_jobs_in_the_named_or_current_repo():
    run = fake_gh({"repos/{owner}/{repo}/actions/runs/11/jobs?per_page=100": {"jobs": FAILED}})
    assert gh_jobs(Target(run="11"), run=run) == FAILED
    assert run.calls[0][0] == ["gh", "api", "repos/{owner}/{repo}/actions/runs/11/jobs?per_page=100"]
    assert run.calls[0][1] == {"capture_output": True, "text": True, "check": True, "timeout": 8}


def test_gh_jobs_reads_one_job_when_the_rerun_names_a_job():
    run = fake_gh({"repos/o/r/actions/jobs/22": FAILED[0]})
    assert gh_jobs(Target(job="22", repo="o/r"), run=run) == FAILED


def test_gh_jobs_prefers_the_job_over_the_run():
    run = fake_gh({"repos/{owner}/{repo}/actions/jobs/22": FAILED[0]})
    assert gh_jobs(Target(run="11", job="22"), run=run) == FAILED


def test_gh_jobs_reads_nothing_without_a_run_or_a_job():
    assert gh_jobs(Target(), run=lambda *a, **k: pytest.fail("called gh")) == []
