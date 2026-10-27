import json
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import ci_budget
from scripts import ci_queue_fail_fast as fail_fast
from scripts import ci_queue_retry as fail_fast_retry

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit


def _jobs():
    from scripts import ci_gate_queue

    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    return ci_gate_queue.normalize({"test.yml": workflow})["test.yml"]["jobs"]


def _gate_needs(jobs):
    return set(next(job for job in jobs.values() if job.get("name") == "Gate — Required")["needs"])


def test_the_watcher_names_its_own_job():
    assert fail_fast.SELF == "queue-fail-fast"


@pytest.mark.parametrize(
    ("log", "lost"),
    [
        pytest.param("##[error]The runner has received a shutdown signal.", True, id="runner-shutdown"),
        pytest.param("##[error]Process completed with exit code 1.", False, id="exit-one"),
        pytest.param(
            "##[error]Process completed with exit code 128.\n##[error]The runner has received a shutdown signal.",
            True,
            id="exit-128-then-shutdown",
        ),
        pytest.param(
            "##[error]Process completed with exit code 1.\n##[error]The runner has received a shutdown signal.",
            True,
            id="exit-one-then-shutdown",
        ),
        pytest.param("", True, id="empty-log"),
        pytest.param("##[error]Process completed with exit code 127.", False, id="exit-127"),
        pytest.param("##[error]Process completed with exit code 137.", True, id="exit-137"),
        pytest.param("##[error]Process completed with exit code 143.", True, id="exit-143"),
        pytest.param("E   AssertionError: Process completed with exit code 1", True, id="quoted-exit-one"),
        pytest.param(
            (_ROOT / "tests/fixtures/queue_retry/failure_quoting_a_loss.txt").read_text(),
            False,
            id="failure-quoting-a-loss",
        ),
        pytest.param(
            "== 1 failed, 3 passed in 4.10s ==\n##[error]The runner has received a shutdown signal.",
            False,
            id="failed-result-then-loss",
        ),
        pytest.param(
            "##[error]The runner has received a shutdown signal.\n== 1 failed, 3 passed in 4.10s ==",
            False,
            id="loss-then-failed-result",
        ),
        pytest.param(
            "== 3 passed in 4.10s ==\n##[error]The runner has received a shutdown signal.",
            True,
            id="passed-result-then-loss",
        ),
        pytest.param(
            "== 3 passed, 0 failed in 4.10s ==\n##[error]The runner has received a shutdown signal.",
            True,
            id="zero-failed-result-then-loss",
        ),
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


@pytest.mark.parametrize(
    ("notes", "lost"),
    [
        ([], True),
        (["The runner has received a shutdown signal."], True),
        (["Process completed with exit code 1.", "The runner has received a shutdown signal."], True),
        (["Process completed with exit code 1."], False),
        (["== 1 failed, 3 passed in 4.10s =="], False),
        (["The action 'Run tests' has timed out after 12 minutes."], False),
        (
            ["The action 'Run tests' has timed out after 12 minutes.", "The runner has received a shutdown signal."],
            False,
        ),
    ],
    ids=[
        "no-annotations",
        "runner-shutdown",
        "exit-one-then-shutdown",
        "exit-one",
        "test-failure",
        "step-timeout",
        "step-timeout-then-shutdown",
    ],
)
def test_missing_logs_use_annotations_before_the_watcher_allows_recovery(monkeypatch, notes, lost):
    calls = []

    def api(path, *flags):
        calls.append(path)
        if path == "repos/o/r/actions/jobs/41/logs":
            raise subprocess.CalledProcessError(1, ["gh", "api", path])
        assert path == "repos/o/r/check-runs/41/annotations"
        return json.dumps([{"message": note} for note in notes])

    monkeypatch.setattr(fail_fast_retry, "_api", api)
    seen = {}
    assert fail_fast.logged_loss("o/r", {"id": 41}, seen) is lost
    assert calls == ["repos/o/r/actions/jobs/41/logs", "repos/o/r/check-runs/41/annotations"]
    assert seen == {}


def test_a_judged_shard_log_is_not_read_again(monkeypatch):
    monkeypatch.setattr(fail_fast_retry, "_api", lambda *args: pytest.fail("read again"))
    assert fail_fast.logged_loss("o/r", {"id": 41}, {"41": False}) is False


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
        "mutation-stats-seed",
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
