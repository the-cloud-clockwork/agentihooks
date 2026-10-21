import json
import subprocess
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
HEAD = "810e2717650390eb596adff102841b05c86eff69"
PULL_QUERY = (
    'query{repository(owner:"o",name:"r"){pullRequest(number:3259){'
    "id state isInMergeQueue commits(last:1){nodes{commit{oid committedDate}}} "
    "timelineItems(last:100,itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT])"
    "{nodes{... on RemovedFromMergeQueueEvent{createdAt reason}}}}}}"
)


def pull(removals):
    return {
        "state": "OPEN",
        "isInMergeQueue": False,
        "id": "PR_kwDO3259",
        "commits": {"nodes": [{"commit": {"oid": HEAD, "committedDate": "2026-10-14T23:06:11Z"}}]},
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


def test_a_shard_lost_again_on_its_in_run_rerun_is_not_requeued():
    failed = [*lost_run(), job("unit rerun (3.11, 2)", SHUTDOWN), job("unit rerun (3.11, 5)", LOST)]
    retry, reason = ci_queue_retry.decide(RUN, failed, WORKFLOW)
    assert not retry
    assert reason == "failed again on its in-run rerun: unit rerun (3.11, 2), unit rerun (3.11, 5)"


def test_a_timed_out_rerun_is_listed_so_it_refuses_the_requeue(monkeypatch):
    jobs = {
        "total_count": 2,
        "jobs": [
            {"id": 1, "name": "unit (3.12, 2)", "conclusion": "failure"},
            {"id": 2, "name": "unit rerun (3.12, 2)", "conclusion": "timed_out"},
        ],
    }

    def api(*args):
        if args[0].endswith("jobs?filter=latest&per_page=100"):
            return json.dumps(jobs)
        return "##[error]" + SHUTDOWN if args[0].endswith("/logs") else "[]"

    monkeypatch.setattr(ci_queue_retry, "_api", api)
    failed = ci_queue_retry.failed_jobs("o/r", 7)
    assert [entry["name"] for entry in failed] == ["unit (3.12, 2)", "unit rerun (3.12, 2)"]
    assert ci_queue_retry.decide(RUN, failed, WORKFLOW) == (
        False,
        "failed again on its in-run rerun: unit rerun (3.12, 2)",
    )


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
        {**RUN, "conclusion": "success"},
    ],
)
def test_only_a_first_failed_merge_group_attempt_is_retried(run):
    assert not ci_queue_retry.decide(run, lost_run(), WORKFLOW)[0]


def test_a_lost_shard_in_a_run_cancelled_by_fail_fast_is_retried():
    cancelled = {**RUN, "conclusion": "cancelled"}
    retry, reason = ci_queue_retry.decide(cancelled, lost_run(), WORKFLOW)
    assert retry, reason


def test_a_cancelled_run_with_no_failed_shard_is_not_retried():
    cancelled = {**RUN, "conclusion": "cancelled"}
    assert ci_queue_retry.decide(cancelled, [], WORKFLOW) == (False, "no unit shard failed")


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
        "total_count": 3,
        "jobs": [
            {"id": 2, "name": "unit (3.11, 3)", "conclusion": "success"},
            {"id": 1, "name": "unit (3.11, 2)", "conclusion": "failure"},
            {"id": 3, "name": "Gate — Required", "conclusion": "failure"},
        ],
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
        return pages[args[0]]

    monkeypatch.setattr(ci_queue_retry, "_api", api)
    monkeypatch.chdir(ROOT)
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert ci_queue_retry.main(["--repo", "o/r", "--run-id", "9"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "run": 9,
        "pull": 3259,
        "requeue": True,
        "reason": "runner lost on unit (3.11, 2); first failed checks removal of this head",
    }
    assert output.read_text() == f"requeue=true\npull=3259\nnode=PR_kwDO3259\nhead={HEAD}\n"
    assert ("graphql", "-f", f"query={PULL_QUERY}") in asked
    assert ("repos/o/r/actions/jobs/1/logs", "--allow-escape-sequences") in asked


def test_a_signature_quoted_in_test_output_is_not_a_runner_loss(monkeypatch):
    jobs = {"total_count": 1, "jobs": [{"id": 1, "name": "unit (3.11, 2)", "conclusion": "failure"}]}
    quoted = "E   AssertionError: 'The runner has received a shutdown signal' != ''\n##[error]Process completed with exit code 1."
    pages = {
        "repos/o/r/actions/runs/9/jobs?filter=latest&per_page=100": json.dumps(jobs),
        "repos/o/r/check-runs/1/annotations": "[]",
        "repos/o/r/actions/jobs/1/logs": quoted,
    }
    monkeypatch.setattr(ci_queue_retry, "_api", lambda path, *flags: pages[path])
    failed = ci_queue_retry.failed_jobs("o/r", 9)
    assert failed == [{"name": "unit (3.11, 2)", "text": "##[error]Process completed with exit code 1."}]
    assert not ci_queue_retry.decide(RUN, failed, WORKFLOW)[0]


def test_colour_codes_in_the_shard_log_keep_the_runner_loss(monkeypatch):
    jobs = {"total_count": 1, "jobs": [{"id": 1, "name": "unit (3.11, 2)", "conclusion": "failure"}]}
    coloured = "\x1b[36;1mpytest -n auto\x1b[0m\n" + SHUTDOWN.replace("##[error]", "\x1b[31m##[error]") + "\x1b[0m"
    pages = {
        "repos/o/r/actions/runs/9/jobs?filter=latest&per_page=100": json.dumps(jobs),
        "repos/o/r/check-runs/1/annotations": "[]",
        "repos/o/r/actions/jobs/1/logs": coloured,
    }
    monkeypatch.setattr(ci_queue_retry, "_api", lambda path, *flags: pages[path])
    failed = ci_queue_retry.failed_jobs("o/r", 9)
    assert ci_queue_retry.decide(RUN, failed, WORKFLOW) == (True, "runner lost on unit (3.11, 2)")


def cancelled_run_pages(lost_log):
    jobs = {
        "total_count": 4,
        "jobs": [
            {"id": 1, "name": "unit (3.12, 2)", "conclusion": "cancelled"},
            {"id": 2, "name": "unit (3.12, 3)", "conclusion": "cancelled"},
            {"id": 3, "name": "shard-check", "conclusion": "cancelled"},
            {"id": 4, "name": "Gate — Required", "conclusion": "failure"},
        ],
    }
    return {
        "repos/o/r/actions/runs/9/jobs?filter=latest&per_page=100": json.dumps(jobs),
        "repos/o/r/check-runs/1/annotations": json.dumps([{"message": "The operation was canceled."}]),
        "repos/o/r/check-runs/2/annotations": json.dumps([{"message": "The operation was canceled."}]),
        "repos/o/r/check-runs/4/annotations": "[]",
        "repos/o/r/actions/jobs/1/logs": lost_log,
        "repos/o/r/actions/jobs/2/logs": "##[error]The operation was canceled.",
    }


def test_a_lost_shard_that_ends_cancelled_is_a_runner_loss(monkeypatch):
    pages = cancelled_run_pages(SHUTDOWN + "##[error]The operation was canceled.")
    monkeypatch.setattr(ci_queue_retry, "_api", lambda path, *flags: pages[path])
    failed = ci_queue_retry.failed_jobs("o/r", 9)
    assert [job["name"] for job in failed] == ["unit (3.12, 2)", "Gate — Required"]
    run = {**RUN, "conclusion": "cancelled"}
    assert ci_queue_retry.decide(run, failed, WORKFLOW) == (True, "runner lost on unit (3.12, 2)")


def test_shards_cancelled_without_a_runner_loss_are_not_retried(monkeypatch):
    pages = cancelled_run_pages("##[error]The operation was canceled.")
    monkeypatch.setattr(ci_queue_retry, "_api", lambda path, *flags: pages[path])
    failed = ci_queue_retry.failed_jobs("o/r", 9)
    assert failed == [{"name": "Gate — Required", "text": ""}]
    run = {**RUN, "conclusion": "cancelled"}
    assert ci_queue_retry.decide(run, failed, WORKFLOW) == (False, "no unit shard failed")


@pytest.mark.parametrize("total, extra", [(1, []), (2, [{"name": "1 unlisted jobs", "text": ""}])])
def test_jobs_beyond_the_listed_page_refuse_the_requeue(monkeypatch, total, extra):
    jobs = {"total_count": total, "jobs": [{"id": 1, "name": "unit (3.11, 2)", "conclusion": "failure"}]}
    pages = {
        "repos/o/r/actions/runs/9/jobs?filter=latest&per_page=100": json.dumps(jobs),
        "repos/o/r/check-runs/1/annotations": json.dumps([{"message": "a"}, {"message": "b"}]),
        "repos/o/r/actions/jobs/1/logs": SHUTDOWN,
    }
    monkeypatch.setattr(ci_queue_retry, "_api", lambda path, *flags: pages[path])
    failed = ci_queue_retry.failed_jobs("o/r", 9)
    errors = [line for line in SHUTDOWN.splitlines() if "##[error]" in line]
    assert failed == [{"name": "unit (3.11, 2)", "text": "\n".join(["a", "b", *errors])}, *extra]
    assert ci_queue_retry.decide(RUN, failed, WORKFLOW)[0] == (not extra)


def test_the_retry_workflow_requeues_unless_enforcement_is_switched_off():
    workflow = yaml.safe_load((ROOT / ".github/workflows/queue-retry.yml").read_text())
    assert workflow[True] == {"workflow_run": {"workflows": ["Tests"], "types": ["completed"]}}
    retry = workflow["jobs"]["retry"]
    for term in ("event == 'merge_group'", "conclusion == 'failure'", "conclusion == 'cancelled'", "run_attempt == 1"):
        assert term in retry["if"]
    assert "write" not in retry["permissions"].values()
    steps = {step.get("name"): step for step in retry["steps"]}
    assert "ci_queue_retry" in steps["Classify the failed jobs"]["run"]
    log = steps["Log the requeue a runner loss would make"]
    assert "vars.QUEUE_RETRY_ENFORCE == 'false'" in log["if"]
    requeue = steps["Requeue the pull request once"]
    assert requeue["if"] == "steps.classify.outputs.requeue == 'true' && vars.QUEUE_RETRY_ENFORCE != 'false'"
    assert steps["Mint the TCC Main CI queue token"]["if"] == requeue["if"]
    assert requeue["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert requeue["env"]["NODE"] == "${{ steps.classify.outputs.node }}"
    assert requeue["env"]["HEAD"] == "${{ steps.classify.outputs.head }}"
    assert requeue["run"].startswith("gh api graphql -f query='mutation($pull: ID!, $head: GitObjectID!)")
    assert "enqueuePullRequest(input: {pullRequestId: $pull, expectedHeadOid: $head})" in requeue["run"]
    assert requeue["run"].endswith('-f pull="$NODE" -f head="$HEAD"')
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


def test_a_head_last_removed_for_another_reason_is_not_requeued():
    merged = {**REMOVED, "createdAt": "2026-10-15T06:00:00Z", "reason": "merged"}
    dequeued = {**REMOVED, "createdAt": "2026-10-15T06:10:00Z", "reason": "manually_removed"}
    assert ci_queue_retry.requeue(pull([REMOVED, merged, dequeued])) == (
        False,
        "head last left the queue for manually_removed",
    )


@pytest.mark.parametrize("change", [{"state": "MERGED"}, {"state": "CLOSED"}, {"isInMergeQueue": True}])
def test_a_pull_request_merged_closed_or_queued_again_is_left_alone(change):
    assert not ci_queue_retry.requeue({**pull([REMOVED]), **change})[0]


def test_the_decision_names_its_reason():
    assert ci_queue_retry.decide({**RUN, "event": "push"}, [], WORKFLOW) == (False, "not a failed merge group run")
    assert ci_queue_retry.decide({**RUN, "run_attempt": 2}, [], WORKFLOW) == (False, "attempt 2 already ran")
    assert ci_queue_retry.decide(RUN, lost_run(), WORKFLOW) == (True, "runner lost on unit (3.11, 2)")
    two_lost = [job("unit (3.12, 7)", LOST), job("unit (3.12, 9)", SHUTDOWN)]
    assert ci_queue_retry.decide(RUN, two_lost, WORKFLOW) == (True, "runner lost on unit (3.12, 7), unit (3.12, 9)")
    two_red = [job("unit (3.11, 2)", RED), job("unit (3.12, 4)", RED)]
    assert ci_queue_retry.decide(RUN, two_red, WORKFLOW) == (
        False,
        "failed without a runner loss signature: unit (3.11, 2), unit (3.12, 4)",
    )
    outside = [*lost_run(), job("lint", ""), job("durations", "")]
    assert ci_queue_retry.decide(RUN, outside, WORKFLOW) == (False, "failed outside the unit shards: lint, durations")


def test_judges_follow_needs_through_every_level():
    workflow = {
        "jobs": {
            "b": {"needs": ["a"]},
            "a": {"needs": "unit", "name": "A"},
            "unit": {},
            "lint": {},
            "c": {"needs": ["lint"]},
        }
    }
    assert ci_queue_retry.dependents(workflow) == {"A", "b"}


def test_gh_api_runs_checked_with_text_output(monkeypatch):
    calls = []

    def run(argv, **options):
        calls.append((argv, options))
        return subprocess.CompletedProcess(argv, 0, stdout="answer")

    monkeypatch.setattr(ci_queue_retry.subprocess, "run", run)
    assert ci_queue_retry._api("repos/o/r", "-f", "x=1") == "answer"
    assert calls == [
        (["gh", "api", "repos/o/r", "-f", "x=1"], {"stdout": subprocess.PIPE, "text": True, "check": True})
    ]


def test_the_pull_request_query_reads_state_head_and_removals(monkeypatch):
    asked = []
    answer = {"data": {"repository": {"pullRequest": {"state": "OPEN"}}}}
    monkeypatch.setattr(ci_queue_retry, "_api", lambda *args: asked.append(args) or json.dumps(answer))
    assert ci_queue_retry.pull_state("o/r", 3259) == {"state": "OPEN"}
    assert asked == [("graphql", "-f", f"query={PULL_QUERY}")]


def test_requeue_reasons_and_a_removal_at_the_head_commit_counts():
    assert ci_queue_retry.requeue(pull([])) == (True, "first failed checks removal of this head")
    queued = {**pull([]), "isInMergeQueue": True}
    assert ci_queue_retry.requeue(queued) == (False, "pull request is not open outside the queue")
    at_head = {**REMOVED, "createdAt": "2026-10-14T23:06:11Z"}
    assert ci_queue_retry.requeue(pull([at_head, REMOVED])) == (
        False,
        "head already left the queue 2 times on failed checks",
    )


@pytest.mark.parametrize("argv", [[], ["--repo", "o/r"], ["--run-id", "9"]])
def test_main_needs_the_repo_and_the_run(argv):
    with pytest.raises(SystemExit):
        ci_queue_retry.main(argv)


def test_a_lost_shard_outside_a_queue_branch_is_not_requeued(monkeypatch, tmp_path, capsys):
    jobs = {"total_count": 1, "jobs": [{"id": 1, "name": "unit (3.11, 2)", "conclusion": "failure"}]}
    pages = {
        "repos/o/r/actions/runs/9": json.dumps({**RUN, "head_branch": "dev"}),
        "repos/o/r/actions/runs/9/jobs?filter=latest&per_page=100": json.dumps(jobs),
        "repos/o/r/check-runs/1/annotations": "[]",
        "repos/o/r/actions/jobs/1/logs": SHUTDOWN,
    }
    monkeypatch.setattr(ci_queue_retry, "_api", lambda path, *flags: pages[path])
    monkeypatch.chdir(ROOT)
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert ci_queue_retry.main(["--repo", "o/r", "--run-id", "9"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "run": 9,
        "pull": None,
        "requeue": False,
        "reason": "no pull request in queue branch dev",
    }
    assert output.read_text() == "requeue=false\npull=\nnode=\nhead=\n"


def queued_then(states, monkeypatch):
    asked, slept = [], []

    def state(repo, number):
        asked.append((repo, number))
        return states[min(len(asked), len(states)) - 1]

    monkeypatch.setattr(ci_queue_retry, "pull_state", state)
    monkeypatch.setattr(ci_queue_retry.time, "sleep", slept.append)
    return asked, slept


def test_the_requeue_waits_for_the_queue_to_drop_the_pull_request(monkeypatch):
    queued = {**pull([REMOVED]), "isInMergeQueue": True}
    left = pull([REMOVED])
    asked, slept = queued_then([queued, queued, left], monkeypatch)
    assert ci_queue_retry.pull_after_exit("o/r", 3259) is left
    assert asked == [("o/r", 3259)] * 3
    assert slept == [30, 30]
    assert ci_queue_retry.requeue(left) == (True, "first failed checks removal of this head")


def test_a_pull_request_still_queued_after_thirty_minutes_is_not_requeued(monkeypatch):
    queued = {**pull([REMOVED]), "isInMergeQueue": True}
    asked, slept = queued_then([queued], monkeypatch)
    assert ci_queue_retry.pull_after_exit("o/r", 3259) is queued
    assert len(asked) == 61
    assert slept == [30] * 60
    assert ci_queue_retry.requeue(queued) == (False, "pull request is not open outside the queue")


def test_main_requeues_once_the_queue_drops_the_pull_request(monkeypatch, tmp_path, capsys):
    jobs = {"total_count": 1, "jobs": [{"id": 1, "name": "unit (3.11, 2)", "conclusion": "failure"}]}
    pages = {
        "repos/o/r/actions/runs/9": json.dumps({**RUN, "id": 9, "head_branch": QUEUE_BRANCH}),
        "repos/o/r/actions/runs/9/jobs?filter=latest&per_page=100": json.dumps(jobs),
        "repos/o/r/check-runs/1/annotations": "[]",
        "repos/o/r/actions/jobs/1/logs": SHUTDOWN,
    }
    monkeypatch.setattr(ci_queue_retry, "_api", lambda path, *flags: pages[path])
    asked, slept = queued_then([{**pull([REMOVED]), "isInMergeQueue": True}, pull([REMOVED])], monkeypatch)
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    assert ci_queue_retry.main(["--repo", "o/r", "--run-id", "9"]) == 0
    assert json.loads(capsys.readouterr().out)["requeue"] is True
    assert asked == [("o/r", 3259)] * 2
    assert slept == [30]
