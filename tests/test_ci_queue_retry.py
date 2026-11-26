import json
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

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
QUOTED_LOSS = (FIXTURES / "failure_quoting_a_loss.txt").read_text()
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


def test_main_reads_the_run_and_writes_the_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = {
        "total_count": 3,
        "jobs": [
            {"id": 2, "name": "unit (3.12, 3)", "conclusion": "success"},
            {"id": 1, "name": "unit (3.12, 2)", "conclusion": "failure"},
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
        "waiting": False,
        "reason": "runner lost on unit (3.12, 2); first failed checks removal of this head",
    }
    assert output.read_text() == f"requeue=true\nwaiting=false\npull=3259\nnode=PR_kwDO3259\nhead={HEAD}\n"
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


def test_a_shard_whose_tests_reported_failures_is_not_lost_whatever_its_output_quotes(monkeypatch):
    jobs = {"total_count": 1, "jobs": [{"id": 1, "name": "unit (3.12, 13)", "conclusion": "failure"}]}
    quoted = "Process completed with exit code 1.\\n##[error]The runner has received a shutdown signal.-True]"
    pages = {
        "repos/o/r/actions/runs/9/jobs?filter=latest&per_page=100": json.dumps(jobs),
        "repos/o/r/check-runs/1/annotations": json.dumps(
            [{"message": quoted}, {"message": "Process completed with exit code 1."}]
        ),
        "repos/o/r/actions/jobs/1/logs": QUOTED_LOSS,
    }
    monkeypatch.setattr(ci_queue_retry, "_api", lambda path, *flags: pages[path])
    failed = ci_queue_retry.failed_jobs("o/r", 9)
    assert ci_queue_retry.decide(RUN, failed, WORKFLOW) == (
        False,
        "failed without a runner loss signature: unit (3.12, 13)",
    )


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


def unreadable_log_api(conclusion, annotations):
    jobs = {"total_count": 1, "jobs": [{"id": 1, "name": "unit (3.12, 13)", "conclusion": conclusion}]}
    pages = {
        "repos/o/r/actions/runs/9/jobs?filter=latest&per_page=100": json.dumps(jobs),
        "repos/o/r/check-runs/1/annotations": json.dumps([{"message": note} for note in annotations]),
    }

    def api(path, *flags):
        if path not in pages:
            raise subprocess.CalledProcessError(1, ["gh", "api", path])
        return pages[path]

    return api


def test_an_unreadable_shard_log_is_a_runner_loss(monkeypatch):
    monkeypatch.setattr(ci_queue_retry, "_api", unreadable_log_api("cancelled", []))
    failed = ci_queue_retry.failed_jobs("o/r", 9)
    assert failed == [{"name": "unit (3.12, 13)", "text": "The job log could not be read"}]
    run = {**RUN, "conclusion": "cancelled"}
    assert ci_queue_retry.decide(run, failed, WORKFLOW) == (True, "runner lost on unit (3.12, 13)")


def test_an_unreadable_log_beside_a_step_exit_is_not_a_runner_loss(monkeypatch):
    monkeypatch.setattr(ci_queue_retry, "_api", unreadable_log_api("failure", ["Process completed with exit code 1."]))
    failed = ci_queue_retry.failed_jobs("o/r", 9)
    assert failed == [{"name": "unit (3.12, 13)", "text": "Process completed with exit code 1."}]
    assert not ci_queue_retry.decide(RUN, failed, WORKFLOW)[0]


@pytest.mark.parametrize("unreadable", [False, True])
@pytest.mark.parametrize("loss", ["", "The runner has received a shutdown signal."], ids=["no-loss", "runner-shutdown"])
def test_a_step_timeout_is_not_retried_even_with_a_loss_signature(monkeypatch, unreadable, loss):
    timeout = "The action 'Run tests' has timed out after 12 minutes."
    api = unreadable_log_api("failure", [timeout, loss])

    def read(path, *flags):
        if not unreadable and path.endswith("/logs"):
            return f"##[error]{timeout}\n##[error]{loss}"
        return api(path, *flags)

    monkeypatch.setattr(ci_queue_retry, "_api", read)
    failed = ci_queue_retry.failed_jobs("o/r", 9)
    assert ci_queue_retry.decide(RUN, failed, WORKFLOW) == (
        False,
        "failed without a runner loss signature: unit (3.12, 13)",
    )


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


def test_a_cancelled_shard_whose_tests_reported_failures_keeps_the_run_red(monkeypatch):
    pages = cancelled_run_pages(QUOTED_LOSS + "##[error]The operation was canceled.")
    monkeypatch.setattr(ci_queue_retry, "_api", lambda path, *flags: pages[path])
    failed = ci_queue_retry.failed_jobs("o/r", 9)
    assert [job["name"] for job in failed] == ["unit (3.12, 2)", "Gate — Required"]
    run = {**RUN, "conclusion": "cancelled"}
    assert ci_queue_retry.decide(run, failed, WORKFLOW) == (
        False,
        "failed without a runner loss signature: unit (3.12, 2)",
    )


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


def test_the_retry_workflow_requeues_unless_enforcement_is_switched_off() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/queue-retry.yml").read_text())
    assert workflow[True] == {
        "workflow_run": {"workflows": ["Tests"], "types": ["completed"], "branches": ["gh-readonly-queue/**"]},
        "pull_request_target": {"types": ["dequeued"]},
    }
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
        (["gh", "api", "repos/o/r", "-f", "x=1"], {"capture_output": True, "check": True, "timeout": 120, "text": True})
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


def test_a_lost_shard_outside_a_queue_branch_is_not_requeued(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = {"total_count": 1, "jobs": [{"id": 1, "name": "unit (3.12, 2)", "conclusion": "failure"}]}
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
        "waiting": False,
        "reason": "no pull request in queue branch dev",
    }
    assert output.read_text() == "requeue=false\nwaiting=false\npull=\nnode=\nhead=\n"


def retry_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    states: list[dict[str, Any]],
    pages: list[list[dict[str, Any]]] | None = None,
    failure: str = SHUTDOWN,
) -> list[str]:
    calls: list[str] = []
    run = {**RUN, "id": 9, "head_branch": QUEUE_BRANCH, "created_at": REMOVED["createdAt"]}
    inventory = pages if pages is not None else [[run]]

    def api(path: str, *flags: str) -> str:
        calls.append(path)
        if path == "repos/o/r/actions/runs/9":
            return json.dumps(run)
        query = parse_qs(urlsplit(path).query)
        assert path.startswith("repos/o/r/actions/workflows/test.yml/runs?")
        assert query["event"] == ["merge_group"]
        assert query["per_page"] == ["100"]
        assert query["created"] == [">=2026-10-14T23:06:11Z"]
        page = int(query.get("page", ["1"])[0])
        rows = inventory[page - 1] if page <= len(inventory) else []
        return json.dumps({"total_count": 100 * (len(inventory) - 1) + len(inventory[-1]), "workflow_runs": rows})

    def state(repo: str, number: int) -> dict[str, Any]:
        assert repo == "o/r"
        calls.append(f"pull:{number}")
        return states[min(calls.count(f"pull:{number}"), len(states)) - 1]

    def failed(repo: str, run_id: int) -> list[dict[str, Any]]:
        calls.append(f"failed:{run_id}")
        return [job("unit (3.12, 2)", failure)]

    def sleep(seconds: float) -> None:
        pytest.fail("queue retry must return without sleeping")

    monkeypatch.setattr(ci_queue_retry, "_api", api)
    monkeypatch.setattr(ci_queue_retry, "pull_state", state)
    monkeypatch.setattr(ci_queue_retry, "failed_jobs", failed)
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "retry-output"))
    return calls


@pytest.mark.feature("queue-retry-releases-runner")
def test_main_returns_waiting_after_one_queued_pull_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = retry_entry(monkeypatch, tmp_path, [{**pull([REMOVED]), "isInMergeQueue": True}, pull([REMOVED])])
    assert ci_queue_retry.main(["--repo", "o/r", "--run-id", "9"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["waiting"] is True
    assert result["requeue"] is False
    assert calls.count("pull:3259") == 1
    assert (tmp_path / "retry-output").read_text() == "requeue=false\nwaiting=true\npull=3259\nnode=\nhead=\n"


@pytest.mark.feature("queue-retry-releases-runner")
def test_main_requeues_an_exited_pull_with_one_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = retry_entry(monkeypatch, tmp_path, [pull([REMOVED])])
    assert ci_queue_retry.main(["--repo", "o/r", "--run-id", "9"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["requeue"] is True
    assert result["waiting"] is False
    assert calls.count("pull:3259") == 1
    assert f"node=PR_kwDO3259\nhead={HEAD}\n" in (tmp_path / "retry-output").read_text()


@pytest.mark.feature("queue-retry-releases-runner")
def test_resume_selects_the_newest_run_for_the_pull(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate = {**RUN, "id": 12, "head_branch": QUEUE_BRANCH, "created_at": REMOVED["createdAt"]}
    rows = [
        {**candidate, "id": 15, "head_branch": QUEUE_BRANCH.replace("pr-3259-", "pr-32590-")},
        {**candidate, "id": 14, "head_branch": "dev", "event": "push"},
        candidate,
        {**candidate, "id": 9},
    ]
    calls = retry_entry(monkeypatch, tmp_path, [pull([REMOVED])], [rows])
    assert ci_queue_retry.main(["--repo", "o/r", "--pull", "3259"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["run"], result["pull"], result["requeue"]) == (12, 3259, True)
    assert calls.count("pull:3259") == 1
    assert [call for call in calls if call.startswith("failed:")] == ["failed:12"]


@pytest.mark.feature("queue-retry-releases-runner")
@pytest.mark.parametrize(
    ("conclusion", "attempt", "failure"),
    [("success", 1, SHUTDOWN), ("failure", 2, SHUTDOWN), ("failure", 1, RED), (None, 1, SHUTDOWN)],
    ids=["success", "rerun", "test-failure", "unfinished"],
)
def test_resume_never_falls_back_to_an_older_runner_loss(
    conclusion: str | None,
    attempt: int,
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old = {**RUN, "id": 9, "head_branch": QUEUE_BRANCH, "created_at": REMOVED["createdAt"]}
    newest = {**old, "id": 12, "conclusion": conclusion, "run_attempt": attempt}
    calls = retry_entry(monkeypatch, tmp_path, [pull([REMOVED])], [[newest, old]], failure)
    assert ci_queue_retry.main(["--repo", "o/r", "--pull", "3259"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["requeue"] is False
    assert result["waiting"] is False
    assert result["run"] == 12
    assert [call for call in calls if call.startswith("failed:")] == ["failed:12"]


@pytest.mark.feature("queue-retry-releases-runner")
def test_resume_pages_until_the_newest_matching_pull_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = {**RUN, "id": 9, "head_branch": QUEUE_BRANCH, "created_at": REMOVED["createdAt"]}
    unrelated = {**run, "id": 10, "head_branch": QUEUE_BRANCH.replace("pr-3259-", "pr-4-")}
    calls = retry_entry(monkeypatch, tmp_path, [pull([REMOVED])], [[unrelated] * 100, [run]])
    assert ci_queue_retry.main(["--repo", "o/r", "--pull", "3259"]) == 0
    assert json.loads(capsys.readouterr().out)["requeue"] is True
    assert len([call for call in calls if "workflows/test.yml/runs?" in call]) == 2
    assert [call for call in calls if call.startswith("failed:")] == ["failed:9"]


@pytest.mark.feature("queue-retry-releases-runner")
def test_resume_without_a_matching_run_never_classifies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = retry_entry(monkeypatch, tmp_path, [pull([REMOVED])], [[]])
    assert ci_queue_retry.main(["--repo", "o/r", "--pull", "3259"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["requeue"] is False
    assert result["waiting"] is False
    assert result["reason"] == "no merge group run for this pull request head"
    assert not any(call.startswith("failed:") for call in calls)


@pytest.mark.feature("queue-retry-releases-runner")
@pytest.mark.parametrize("boundary", ["last-page", "empty-page", "page-limit"])
def test_resume_stops_at_inventory_boundaries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], boundary: str
) -> None:
    unrelated = {**RUN, "id": 10, "head_branch": QUEUE_BRANCH.replace("pr-3259-", "pr-4-")}
    inventories = {
        "last-page": [[unrelated] * 100],
        "empty-page": [[], [unrelated]],
        "page-limit": [[unrelated] * 100] * (ci_queue_retry.RUN_PAGES + 1),
    }
    expected = {"last-page": 1, "empty-page": 1, "page-limit": ci_queue_retry.RUN_PAGES}
    calls = retry_entry(monkeypatch, tmp_path, [pull([REMOVED])], inventories[boundary])
    assert ci_queue_retry.main(["--repo", "o/r", "--pull", "3259"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["requeue"] is False
    assert result["reason"] == "no merge group run for this pull request head"
    assert len([call for call in calls if "workflows/test.yml/runs?" in call]) == expected[boundary]
    assert not any(call.startswith("failed:") for call in calls)


@pytest.mark.feature("queue-retry-releases-runner")
@pytest.mark.parametrize(
    "state",
    [
        {**pull([REMOVED]), "state": "MERGED"},
        {**pull([REMOVED]), "state": "CLOSED"},
        {**pull([REMOVED]), "isInMergeQueue": True},
        pull([REMOVED, {**REMOVED, "createdAt": "2026-10-15T05:41:00Z"}]),
        pull([{**REMOVED, "reason": "manual"}]),
    ],
    ids=["merged", "closed", "queued", "second-removal", "manual"],
)
def test_resume_keeps_the_existing_pull_guards(
    state: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = retry_entry(monkeypatch, tmp_path, [state])
    assert ci_queue_retry.main(["--repo", "o/r", "--pull", "3259"]) == 0
    assert json.loads(capsys.readouterr().out)["requeue"] is False
    assert calls.count("pull:3259") == 1


@pytest.mark.feature("queue-retry-releases-runner")
@pytest.mark.parametrize("dequeued_first", [False, True], ids=["completed-first", "dequeued-first"])
def test_either_event_order_resumes_once_the_run_and_pull_are_ready(
    dequeued_first: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = {**RUN, "id": 9, "head_branch": QUEUE_BRANCH, "created_at": REMOVED["createdAt"]}
    if dequeued_first:
        calls = retry_entry(monkeypatch, tmp_path, [pull([REMOVED])], [[{**run, "conclusion": None}]])
        first = ["--pull", "3259"]
        second = ["--run-id", "9"]
    else:
        calls = retry_entry(monkeypatch, tmp_path, [{**pull([REMOVED]), "isInMergeQueue": True}])
        first = ["--run-id", "9"]
        second = ["--pull", "3259"]
    assert ci_queue_retry.main(["--repo", "o/r", *first]) == 0
    assert json.loads(capsys.readouterr().out)["requeue"] is False
    calls.clear()
    monkeypatch.setattr(ci_queue_retry, "pull_state", lambda repo, number: pull([REMOVED]))
    assert ci_queue_retry.main(["--repo", "o/r", *second]) == 0
    assert json.loads(capsys.readouterr().out)["requeue"] is True
    calls.clear()
    monkeypatch.setattr(ci_queue_retry, "pull_state", lambda repo, number: {**pull([REMOVED]), "isInMergeQueue": True})
    assert ci_queue_retry.main(["--repo", "o/r", "--pull", "3259"]) == 0
    assert json.loads(capsys.readouterr().out)["requeue"] is False


@pytest.mark.feature("queue-retry-releases-runner")
def test_retry_workflow_releases_the_runner_and_resumes_from_trusted_code() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/queue-retry.yml").read_text())
    assert workflow[True]["pull_request_target"] == {"types": ["dequeued"]}
    retry = workflow["jobs"]["retry"]
    assert retry["timeout-minutes"] <= 5
    assert "github.event_name == 'pull_request_target'" in retry["if"]
    checkout = next(step for step in retry["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    classify = next(step for step in retry["steps"] if step.get("id") == "classify")
    assert classify["env"]["PULL"] == "${{ github.event.pull_request.number }}"
    assert '--pull "$PULL"' in classify["run"]
    assert '--run-id "$RUN_ID"' in classify["run"]


@pytest.mark.feature("queue-retry-releases-runner")
def test_retry_workflow_timeout_is_at_most_five_minutes() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/queue-retry.yml").read_text())
    assert workflow["jobs"]["retry"]["timeout-minutes"] <= 5
