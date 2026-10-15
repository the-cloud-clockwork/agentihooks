import json
from pathlib import Path

import pytest
import yaml

from scripts import ci_queue_retry

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/queue_retry"
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
RUN = {"event": "merge_group", "conclusion": "failure", "run_attempt": 1}
SHUTDOWN = (FIXTURES / "shutdown_signal.txt").read_text()
LOST = (FIXTURES / "lost_communication.txt").read_text()
RED = (FIXTURES / "test_failure.txt").read_text()


def job(name, text):
    return {"name": name, "text": text}


def lost_run():
    return [
        job("unit (3.11, 2)", "The operation was canceled.\n" + SHUTDOWN),
        job("shard-check (3.11)", "Process completed with exit code 1."),
        job("Gate — Required", "Process completed with exit code 1."),
    ]


def test_a_shutdown_signal_on_a_unit_shard_is_retried():
    retry, reason = ci_queue_retry.decide(RUN, lost_run(), WORKFLOW)
    assert retry, reason
    assert "unit (3.11, 2)" in reason


def test_a_lost_communication_annotation_is_retried():
    failed = [job("unit (3.12, 7)", LOST), job("unit (3.12, 9)", SHUTDOWN), job("Gate — Required", "")]
    assert ci_queue_retry.decide(RUN, failed, WORKFLOW)[0]


def test_a_real_test_failure_is_not_retried():
    failed = [job("unit (3.11, 2)", RED), job("Gate — Required", "")]
    retry, reason = ci_queue_retry.decide(RUN, failed, WORKFLOW)
    assert not retry
    assert "without a runner loss signature" in reason


def test_one_red_shard_beside_a_lost_one_is_not_retried():
    failed = [*lost_run(), job("unit (3.12, 4)", RED)]
    assert not ci_queue_retry.decide(RUN, failed, WORKFLOW)[0]


@pytest.mark.parametrize("name", ["lint", "test-count (3.12)", "worker-image / worker-health", "durations"])
def test_a_failure_outside_the_unit_shards_is_not_retried(name):
    retry, reason = ci_queue_retry.decide(RUN, [*lost_run(), job(name, SHUTDOWN)], WORKFLOW)
    assert not retry
    assert name in reason


@pytest.mark.parametrize(
    "run",
    [
        {**RUN, "run_attempt": 2},
        {**RUN, "event": "pull_request"},
        {**RUN, "event": "push"},
        {**RUN, "conclusion": "cancelled"},
    ],
)
def test_only_a_first_failed_merge_group_attempt_is_retried(run):
    assert not ci_queue_retry.decide(run, lost_run(), WORKFLOW)[0]


def test_a_run_whose_only_failures_are_judges_is_not_retried():
    failed = [job("shard-check (3.11)", SHUTDOWN), job("Gate — Required", SHUTDOWN)]
    retry, reason = ci_queue_retry.decide(RUN, failed, WORKFLOW)
    assert not retry
    assert reason == "no unit shard failed"


def test_judges_are_the_jobs_that_wait_on_the_unit_shards():
    downstream = ci_queue_retry.dependents(WORKFLOW)
    assert {"shard-check", "Gate — Required"} <= downstream
    assert not {"unit", "lint", "durations", "split", "test-count"} & downstream


def test_main_reads_the_run_and_writes_the_decision(monkeypatch, tmp_path, capsys):
    jobs = {
        "jobs": [
            {"id": 1, "name": "unit (3.11, 2)", "conclusion": "failure"},
            {"id": 2, "name": "unit (3.11, 3)", "conclusion": "success"},
            {"id": 3, "name": "Gate — Required", "conclusion": "failure"},
        ]
    }
    pages = {
        "repos/o/r/actions/runs/9": json.dumps({**RUN, "id": 9}),
        "repos/o/r/actions/runs/9/jobs?filter=latest&per_page=100": json.dumps(jobs),
        "repos/o/r/check-runs/1/annotations": json.dumps([{"message": "The operation was canceled."}]),
        "repos/o/r/check-runs/3/annotations": "[]",
        "repos/o/r/actions/jobs/1/logs": SHUTDOWN,
    }
    monkeypatch.setattr(ci_queue_retry, "_api", lambda path: pages.get(path, ""))
    monkeypatch.chdir(ROOT)
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert ci_queue_retry.main(["--repo", "o/r", "--run-id", "9"]) == 0
    assert json.loads(capsys.readouterr().out)["retry"] is True
    assert output.read_text() == "retry=true\n"


def test_a_signature_quoted_in_test_output_is_not_a_runner_loss(monkeypatch):
    jobs = {"jobs": [{"id": 1, "name": "unit (3.11, 2)", "conclusion": "failure"}]}
    quoted = "E   AssertionError: 'The runner has received a shutdown signal' != ''\n##[error]Process completed with exit code 1."
    pages = {
        "repos/o/r/actions/runs/9/jobs?filter=latest&per_page=100": json.dumps(jobs),
        "repos/o/r/actions/jobs/1/logs": quoted,
    }
    monkeypatch.setattr(ci_queue_retry, "_api", lambda path: pages.get(path, ""))
    failed = ci_queue_retry.failed_jobs("o/r", 9)
    assert not ci_queue_retry.decide(RUN, failed, WORKFLOW)[0]


def test_the_retry_workflow_reruns_only_a_first_failed_merge_group_attempt():
    workflow = yaml.safe_load((ROOT / ".github/workflows/queue-retry.yml").read_text())
    assert workflow[True] == {"workflow_run": {"workflows": ["Tests"], "types": ["completed"]}}
    retry = workflow["jobs"]["retry"]
    for term in ("event == 'merge_group'", "conclusion == 'failure'", "run_attempt == 1"):
        assert term in retry["if"]
    assert retry["permissions"]["actions"] == "write"
    steps = retry["steps"]
    assert "ci_queue_retry" in steps[-2]["run"]
    assert steps[-1]["if"] == "steps.classify.outputs.retry == 'true'"
    assert steps[-1]["run"].startswith('gh run rerun "$RUN_ID" --failed')
