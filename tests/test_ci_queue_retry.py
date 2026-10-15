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
QUEUE_BRANCH = "gh-readonly-queue/dev/pr-3259-810e2717650390eb596adff102841b05c86eff69"
REMOVED = {"createdAt": "2026-10-15T05:40:23Z", "reason": "failed_checks"}


def pull(removals):
    return {
        "state": "OPEN",
        "isInMergeQueue": False,
        "commits": {"nodes": [{"commit": {"committedDate": "2026-10-14T23:06:11Z"}}]},
        "timelineItems": {"nodes": removals},
    }


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
    run = {**RUN, "id": 9, "head_branch": QUEUE_BRANCH}
    pages = {
        "repos/o/r/actions/runs/9": json.dumps(run),
        "repos/o/r/actions/runs/9/jobs?filter=latest&per_page=100": json.dumps(jobs),
        "repos/o/r/check-runs/1/annotations": json.dumps([{"message": "The operation was canceled."}]),
        "repos/o/r/check-runs/3/annotations": "[]",
        "repos/o/r/actions/jobs/1/logs": SHUTDOWN,
        "graphql": json.dumps({"data": {"repository": {"pullRequest": pull([REMOVED])}}}),
    }
    asked = []

    def api(*args):
        asked.append(args)
        return pages.get(args[0], "")

    monkeypatch.setattr(ci_queue_retry, "_api", api)
    monkeypatch.chdir(ROOT)
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert ci_queue_retry.main(["--repo", "o/r", "--run-id", "9"]) == 0
    assert json.loads(capsys.readouterr().out)["requeue"] is True
    assert output.read_text() == "requeue=true\npull=3259\n"
    query = next(args for args in asked if args[0] == "graphql")[2]
    assert 'owner:"o",name:"r"' in query and "pullRequest(number:3259)" in query


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


def test_the_retry_workflow_requeues_only_when_enforcement_is_on():
    workflow = yaml.safe_load((ROOT / ".github/workflows/queue-retry.yml").read_text())
    assert workflow[True] == {"workflow_run": {"workflows": ["Tests"], "types": ["completed"]}}
    retry = workflow["jobs"]["retry"]
    for term in ("event == 'merge_group'", "conclusion == 'failure'", "run_attempt == 1"):
        assert term in retry["if"]
    assert "write" not in retry["permissions"].values()
    steps = {step.get("name"): step for step in retry["steps"]}
    assert "ci_queue_retry" in steps["Classify the failed jobs"]["run"]
    log = steps["Log the requeue a runner loss would make"]
    assert "vars.QUEUE_RETRY_ENFORCE != 'true'" in log["if"]
    requeue = steps["Requeue the pull request once"]
    assert requeue["if"] == "steps.classify.outputs.requeue == 'true' && vars.QUEUE_RETRY_ENFORCE == 'true'"
    assert steps["Mint the TCC Main CI queue token"]["if"] == requeue["if"]
    assert requeue["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert requeue["run"].startswith('gh pr merge "$PULL" --squash --auto')
    assert not any("rerun" in str(step.get("run", "")) for step in retry["steps"])


def test_the_pull_request_comes_from_the_queue_branch():
    assert ci_queue_retry.pull_number({"head_branch": QUEUE_BRANCH}) == 3259
    assert ci_queue_retry.pull_number({"head_branch": "dev"}) is None
    assert ci_queue_retry.pull_number({}) is None


def test_the_first_failed_checks_removal_of_a_head_requeues():
    assert ci_queue_retry.requeue(pull([REMOVED]))[0]
    assert ci_queue_retry.requeue(pull([]))[0]


def test_a_head_removed_twice_on_failed_checks_is_not_requeued_again():
    retry, reason = ci_queue_retry.requeue(pull([REMOVED, {**REMOVED, "createdAt": "2026-10-15T06:10:00Z"}]))
    assert not retry
    assert "2 times" in reason


def test_removals_before_the_head_commit_or_for_other_reasons_do_not_count():
    earlier = {**REMOVED, "createdAt": "2026-10-14T20:00:00Z"}
    merged = {**REMOVED, "reason": "merged"}
    assert ci_queue_retry.requeue(pull([earlier, merged, REMOVED]))[0]


@pytest.mark.parametrize("change", [{"state": "MERGED"}, {"state": "CLOSED"}, {"isInMergeQueue": True}])
def test_a_pull_request_merged_closed_or_queued_again_is_left_alone(change):
    assert not ci_queue_retry.requeue({**pull([REMOVED]), **change})[0]
