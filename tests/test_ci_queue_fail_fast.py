import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import ci_budget
from scripts import ci_queue_fail_fast as fail_fast
from scripts import ci_queue_retry as fail_fast_retry

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit

_QUEUE = "github.event_name == 'merge_group'"
_GATE_NEED = (
    "${{ !cancelled() && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success')"
    " && (github.event_name != 'merge_group' || needs.reuse.outputs.reused != 'true')"
    " && github.event_name != 'pull_request' }}"
)
_APP_TOKEN = "${{ steps.app-token.outputs.token }}"


def _job(name, conclusion=None, status=None):
    return {"name": name, "status": status or ("completed" if conclusion else "queued"), "conclusion": conclusion}


def _jobs():
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]


def _gate_needs(jobs):
    return set(next(job for job in jobs.values() if job.get("name") == "Gate — Required")["needs"])


def _step(job, name):
    return next(step for step in job["steps"] if step.get("name") == name)


def test_the_watcher_skips_its_own_job_and_waits_on_stage_budget():
    assert fail_fast.SELF == "queue-fail-fast"
    assert fail_fast.LAST == "stage-budget"


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "cancelled"])
def test_a_failed_timed_out_or_cancelled_job_is_named(conclusion):
    jobs = [_job("lint", "success"), _job("unit (3.12, 1)"), _job("swarm-image / smoke", conclusion)]
    assert fail_fast.failed(jobs) == "swarm-image / smoke"


def test_the_first_failed_job_listed_is_named():
    assert fail_fast.failed([_job("lint", "failure"), _job("sonar", "failure")]) == "lint"


def test_a_timed_out_unit_shard_is_named_at_once():
    assert fail_fast.failed([_job("unit (3.12, 4)", "timed_out")], lambda job: True) == "unit (3.12, 4)"


@pytest.mark.parametrize("conclusion", ["failure", "cancelled"])
def test_a_unit_shard_that_lost_its_runner_waits_for_its_rerun(conclusion):
    jobs = [_job("unit (3.12, 4)", conclusion), _job("lint", "success")]
    assert fail_fast.failed(jobs, lambda job: True) is None
    assert fail_fast.verdict(jobs, lambda job: True) == "wait"


def test_a_unit_shard_that_failed_its_tests_is_named():
    jobs = [_job("unit (3.12, 4)", "failure"), _job("unit (3.12, 5)", "failure")]
    assert fail_fast.failed(jobs, lambda job: job["name"] == "unit (3.12, 4)") == "unit (3.12, 5)"


@pytest.mark.parametrize("name", ["unit rerun (3.12, 4)", "unit-lost", "unit-selected", "shard-check (3.12)"])
def test_a_rerun_classifier_or_judge_failure_is_named_even_when_lost(name):
    assert fail_fast.failed([_job(name, "failure")], lambda job: True) == name


@pytest.mark.parametrize(
    ("log", "lost"),
    [
        ("##[error]The runner has received a shutdown signal.", True),
        ("##[error]Process completed with exit code 1.", False),
        ("##[error]Process completed with exit code 128.\n##[error]The runner has received a shutdown signal.", True),
        ("##[error]Process completed with exit code 1.\n##[error]The runner has received a shutdown signal.", True),
        ("", True),
        ("##[error]Process completed with exit code 127.", False),
        ("##[error]Process completed with exit code 137.", True),
        ("##[error]Process completed with exit code 143.", True),
        ("E   AssertionError: Process completed with exit code 1", True),
        ((_ROOT / "tests/fixtures/queue_retry/failure_quoting_a_loss.txt").read_text(), False),
        ("== 1 failed, 3 passed in 4.10s ==\n##[error]The runner has received a shutdown signal.", False),
        ("== 3 passed in 4.10s ==\n##[error]The runner has received a shutdown signal.", True),
    ],
)
def test_only_a_nonzero_step_exit_without_a_loss_signature_is_a_real_failure(monkeypatch, log, lost):
    def api(*args):
        assert args[0] == "repos/o/r/actions/jobs/41/logs"
        return log

    monkeypatch.setattr(fail_fast_retry, "_api", api)
    seen = {}
    assert fail_fast.logged_loss("o/r", {"id": 41}, seen) is lost
    assert seen == {"41": lost}


def test_a_missing_log_waits_and_is_read_again_next_poll(monkeypatch):
    def api(*args):
        raise subprocess.CalledProcessError(1, ["gh", "api", *args])

    monkeypatch.setattr(fail_fast_retry, "_api", api)
    seen = {}
    assert fail_fast.logged_loss("o/r", {"id": 41}, seen) is True
    assert seen == {}


def test_a_judged_shard_log_is_not_read_again(monkeypatch):
    monkeypatch.setattr(fail_fast_retry, "_api", lambda *args: pytest.fail("read again"))
    assert fail_fast.logged_loss("o/r", {"id": 41}, {"41": False}) is False


def test_main_with_a_repository_keeps_each_shard_verdict_between_polls(tmp_path, monkeypatch, capsys):
    reads = []

    def errors(repo, job_id):
        reads.append((repo, job_id))
        return ["##[error]The runner has received a shutdown signal."]

    monkeypatch.setattr(fail_fast, "log_errors", errors)
    listing = _listing(tmp_path, {**_job("unit (3.12, 4)", "failure"), "id": 41}, _job("lint", "success"))
    seen = tmp_path / "seen.json"
    for _ in range(2):
        assert fail_fast.main([str(listing), "--repo", "o/r", "--seen", str(seen)]) == 0
    assert capsys.readouterr().out == "wait\nwait\n"
    assert reads == [("o/r", 41)]
    assert json.loads(seen.read_text()) == {"41": True}


def test_passing_skipped_and_running_jobs_and_the_watcher_itself_name_nothing():
    jobs = [
        _job("lint", "success"),
        _job("mutation", "skipped"),
        _job("unit (3.11, 2)"),
        _job("queue-fail-fast", "cancelled"),
    ]
    assert fail_fast.failed(jobs) is None


@pytest.mark.parametrize(
    "jobs, expected",
    [
        ([_job("lint", "success"), _job("Gate — Required", "success")], False),
        ([_job("stage-budget")], False),
        ([_job("stage-budget", status="in_progress")], False),
        ([_job("lint", "success"), _job("stage-budget", "success")], True),
    ],
)
def test_the_run_settles_only_once_stage_budget_completed(jobs, expected):
    assert fail_fast.settled(jobs) is expected


@pytest.mark.parametrize(
    "jobs, expected",
    [
        ([_job("unit (3.12, 1)"), _job("lint", "success")], "wait"),
        ([_job("stage-budget")], "wait"),
        ([_job("lint", "failure")], "failed=lint"),
        ([_job("stage-budget", "success"), _job("sonar", "failure")], "failed=sonar"),
        ([_job("lint", "success"), _job("stage-budget", "success")], "failed="),
    ],
)
def test_the_verdict_names_a_failure_settles_or_waits(jobs, expected):
    assert fail_fast.verdict(jobs) == expected


def _listing(tmp_path, *jobs):
    path = tmp_path / "jobs.jsonl"
    path.write_text("".join(json.dumps(job) + "\n\n" for job in jobs))
    return path


def test_main_prints_the_verdict_of_a_jobs_listing(tmp_path, capsys):
    assert fail_fast.main([str(_listing(tmp_path, _job("lint", "success"), _job("sonar", "timed_out")))]) == 0
    assert capsys.readouterr().out == "failed=sonar\n"


def test_main_requires_the_jobs_listing():
    with pytest.raises(SystemExit) as raised:
        fail_fast.main([])
    assert raised.value.code == 2


def test_the_module_runs_from_the_command_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["ci_queue_fail_fast", str(_listing(tmp_path, _job("unit (3.11, 1)")))])
    with pytest.raises(SystemExit) as raised:
        runpy.run_module("scripts.ci_queue_fail_fast", run_name="__main__")
    assert raised.value.code == 0
    assert capsys.readouterr().out == "wait\n"


def _watch(tmp_path, reads, heads=None, server_error=False, rebuild_fail=False):
    fake, bin_dir = tmp_path / "fake", tmp_path / "bin"
    fake.mkdir()
    bin_dir.mkdir()
    for index, jobs in enumerate(reads):
        if jobs is not None:
            (fake / f"{index}.json").write_text("".join(json.dumps(job) + "\n" for job in jobs))
            (fake / f"{index}.page").write_text(json.dumps({"jobs": jobs}))
    if heads is not None:
        for index, head in enumerate(heads):
            if head is not None:
                (fake / f"head-{index}").write_text(head)
    scripts = {
        "gh": 'if [[ "$*" == *git/matching-refs/* ]]; then\n'
        '  n=$(cat "$FAKE/ref-count" 2>/dev/null || echo 0)\n'
        '  echo $((n + 1)) > "$FAKE/ref-count"\n'
        '  echo "$*" >> "$FAKE/ref-calls"\n'
        '  if [[ "$FAKE_HEADS" == no ]]; then echo old; exit 0; fi\n'
        '  [[ -f "$FAKE/head-$n" ]] && cat "$FAKE/head-$n"\n'
        "  exit $?\n"
        "fi\n"
        'echo "$*" >> "$FAKE/calls"\n'
        '[[ "$1" != run ]] || exit 0\n'
        'if [[ "$SERVER_ERROR" == true && "$*" != *per_page=5\\&* ]]; then\n'
        '  echo "gh: Server Error (HTTP 500)" >&2; exit 1\n'
        "fi\n"
        'n=$(cat "$FAKE/count" 2>/dev/null || echo 0)\necho $((n + 1)) > "$FAKE/count"\n'
        'if [[ "$*" == *--jq* ]]; then cat "$FAKE/$n.json"; else cat "$FAKE/$n.page"; fi\n',
        "sleep": 'echo "$1" >> "$FAKE/sleeps"\n(( $(wc -l < "$FAKE/sleeps") <= 30 ))\n',
        "python3": 'if [[ "$2" == scripts.ci_queue_rebuild ]]; then\n'
        '  echo "${3-unset}" > "$FAKE/rebuild-arg"\n'
        '  [[ -z "$FAKE_REBUILD_FAIL" ]] || exit 1\n'
        '  echo "record queue-rebuild-7-2: unknown cause: g"\n'
        "  exit 0\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n',
    }
    for name, body in scripts.items():
        (bin_dir / name).write_text("#!/usr/bin/env bash\n" + body)
        (bin_dir / name).chmod(0o755)
    output = tmp_path / "output"
    output.write_text("")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE": str(fake),
        "SERVER_ERROR": str(server_error).lower(),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(output),
        "GITHUB_REPOSITORY": "org/repo",
        "GITHUB_RUN_ID": "7",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": "old",
        "GITHUB_REF": "refs/heads/gh-readonly-queue/dev/pr-1-old",
        "FAKE_HEADS": "no" if heads is None else "yes",
        "FAKE_REBUILD_FAIL": "1" if rebuild_fail else "",
    }
    run = _step(_jobs()["queue-fail-fast"], "Watch for the first failed graded job")["run"]
    result = subprocess.run(["bash", "-e", "-c", run], cwd=_ROOT, env=env, capture_output=True, text=True)
    if server_error and output.read_text().strip() == "failed=lint":
        cancel = _step(_jobs()["queue-fail-fast"], "Cancel this queue run so the queue drops the entry")["run"]
        result = subprocess.run(
            ["bash", "-e", "-c", cancel], cwd=_ROOT, env={**env, "FAILED": "lint"}, capture_output=True, text=True
        )
    sleeps = (fake / "sleeps").read_text().split() if (fake / "sleeps").exists() else []
    calls = (fake / "calls").read_text().splitlines() if (fake / "calls").exists() else []
    return result, output.read_text(), sleeps, calls


@pytest.mark.parametrize("head", ["", "replacement"])
def test_a_deleted_or_replaced_queue_group_stops_before_jobs_finish(tmp_path, head):
    result, output, sleeps, calls = _watch(tmp_path, [[_job("lint")]], heads=[head])
    assert result.returncode == 0, result.stderr
    assert output == "failed=queue group is no longer current (record queue-rebuild-7-2: unknown cause: g)\n"
    assert (tmp_path / "fake" / "rebuild-arg").read_text() == head + "\n"
    assert sleeps == []
    assert calls == []
    ref_call = (tmp_path / "fake" / "ref-calls").read_text()
    assert "git/matching-refs/heads/gh-readonly-queue/dev/pr-1-old" in ref_call
    assert 'select(.ref == "refs/heads/gh-readonly-queue/dev/pr-1-old")' in ref_call


def test_a_failed_rebuild_record_still_cancels_the_stale_group(tmp_path):
    result, output, _, _ = _watch(tmp_path, [[_job("lint")]], heads=["replacement"], rebuild_fail=True)
    assert result.returncode == 0, result.stderr
    assert output == "failed=queue group is no longer current (record unavailable)\n"


def test_a_queue_lookup_error_is_retried_without_cancelling_a_live_group(tmp_path):
    result, output, sleeps, calls = _watch(tmp_path, [[_job("lint", "failure")]], heads=[None, "old"])
    assert result.returncode == 0, result.stderr
    assert output == "failed=lint\n"
    assert sleeps == ["15"]
    assert len(calls) == 1


def test_ten_queue_lookup_errors_do_not_claim_the_group_was_deleted(tmp_path):
    result, output, sleeps, calls = _watch(tmp_path, [], heads=[None] * 10)
    assert result.returncode == 1, result.stderr
    assert output == ""
    assert len(sleeps) == 9
    assert calls == []
    assert "::error::Ten reads" in result.stdout


def test_the_watch_step_polls_this_attempt_until_a_graded_job_fails(tmp_path):
    result, output, sleeps, calls = _watch(tmp_path, [[_job("lint")], [_job("lint", "failure")]])
    assert result.returncode == 0, result.stderr
    assert output == "failed=lint\n"
    assert sleeps == ["15"]
    assert calls == ["api repos/org/repo/actions/runs/7/attempts/2/jobs?per_page=10&page=1"] * 2


def test_the_watch_step_cancels_after_large_job_pages_return_server_errors(tmp_path):
    result, output, sleeps, calls = _watch(tmp_path, [[_job("lint", "failure")]], server_error=True)
    assert result.returncode == 0, result.stderr
    assert output == "failed=lint\n"
    assert any("per_page=5&page=1" in call for call in calls)
    assert all("/attempts/2/jobs?" in call for call in calls[:-1])
    assert calls[-1] == "run cancel 7 -R org/repo"
    assert sleeps == []


def test_the_watch_step_stops_without_a_name_once_every_graded_job_settled(tmp_path):
    result, output, sleeps, _ = _watch(tmp_path, [[_job("lint", "success"), _job("stage-budget", "success")]])
    assert result.returncode == 0, result.stderr
    assert output == "failed=\n"
    assert sleeps == []


def test_the_watch_step_gives_up_quietly_after_ten_failed_reads_in_a_row(tmp_path):
    result, output, sleeps, calls = _watch(tmp_path, [None] * 9 + [[_job("lint")]] + [None] * 10)
    assert result.returncode == 0, result.stderr
    assert output == ""
    assert len(calls) == 20
    assert len(sleeps) == 19
    assert result.stdout.count("::warning::") == 1


def test_the_watcher_is_a_gate_need_that_works_only_on_merge_queue_runs():
    jobs = _jobs()
    job = jobs["queue-fail-fast"]
    assert job["if"] == _GATE_NEED
    assert job["needs"] == ["reuse", "admission"]
    assert "permissions" not in job
    assert job["timeout-minutes"] == 60
    assert job["runs-on"] == jobs["stage-budget"]["runs-on"]
    assert "queue-fail-fast" in _gate_needs(jobs)
    assert "queue-fail-fast" not in jobs["stage-budget"]["needs"]
    assert [step["if"] for step in job["steps"][:3]] == [_QUEUE] * 3


def test_the_watcher_reads_and_cancels_on_the_app_token():
    job = _jobs()["queue-fail-fast"]
    mint = _step(job, "Mint the tcc main ci App token")
    watch = _step(job, "Watch for the first failed graded job")
    cancel = _step(job, "Cancel this queue run so the queue drops the entry")
    assert job["steps"][0] == {
        "uses": "actions/checkout@v4",
        "if": _QUEUE,
        "with": {"ref": "${{ github.sha }}", "persist-credentials": False},
    }
    assert mint["id"] == "app-token"
    assert mint["uses"] == "actions/create-github-app-token@v3.2.0"
    assert mint["with"] == {
        "client-id": "${{ secrets.TCC_CI_CLIENT_ID }}",
        "private-key": "${{ secrets.TCC_CI_APP_PRIVATE_KEY }}",
        "repositories": "${{ github.event.repository.name }}",
        "permission-actions": "write",
        "permission-contents": "read",
        "permission-pull-requests": "read",
    }
    assert watch["id"] == "watch"
    assert watch["env"] == {"GH_TOKEN": _APP_TOKEN}
    assert cancel["if"] == "steps.watch.outputs.failed != ''"
    assert cancel["env"] == {"GH_TOKEN": _APP_TOKEN, "FAILED": "${{ steps.watch.outputs.failed }}"}
    assert cancel["run"].splitlines() == [
        'echo "::error::$FAILED failed, so Gate Required cannot pass; cancelling this queue run."',
        'gh run cancel "$GITHUB_RUN_ID" -R "$GITHUB_REPOSITORY"',
    ]


def test_stage_budget_needs_every_graded_job_so_its_completion_means_all_settled():
    jobs = _jobs()
    assert _gate_needs(jobs) - {"stage-budget", "queue-fail-fast"} <= set(jobs["stage-budget"]["needs"])


def test_every_merge_queue_job_but_the_watcher_is_graded():
    jobs = _jobs()
    push_only = {
        job_id
        for job_id, job in jobs.items()
        if "github.event_name == 'push'" in str(job.get("if", "")) and "merge_group" not in str(job.get("if", ""))
    }
    queued = set(jobs) - push_only - {"admission", "gate-required", "test-summary"}
    expected = {
        "promote",
        "refresh-durations",
        "count-record",
        "flake-budget",
        "coverage-baseline",
        "coverage-ratchet",
        "stage-budget",
        "kind-due",
        "worker-image",
        "swarm-image",
        "brain-smoke",
        "writer-lock",
        "ledger-load",
        "hierarchy-cpu",
    }
    if (_ROOT / ".github/workflows/count-record.yml").exists():
        expected.remove("count-record")
    assert push_only == expected
    assert queued <= _gate_needs(jobs)


def test_the_watcher_budget_covers_its_work_outside_the_queue():
    assert ci_budget.BUDGETS["queue-fail-fast"] == 60


def test_the_stage_report_leaves_out_the_watcher():
    def job(name):
        return {"name": name, "created_at": "2026-10-13T08:00:00Z", "completed_at": "2026-10-13T08:40:00Z"}

    assert ci_budget.stages([job("queue-fail-fast"), job("lint")]) == {"lint": 2400.0}
