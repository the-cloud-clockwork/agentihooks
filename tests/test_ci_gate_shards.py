import subprocess
from unittest.mock import patch

import pytest

from scripts import ci_gate_shards

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("conclusion", [None, "failure", "cancelled", "skipped"])
def test_incomplete_or_failed_shards_never_finish_verification(conclusion):
    jobs = [{"name": "unit (3.12, 1)", "conclusion": "success"}]
    assert not ci_gate_shards.ready(jobs, [1, 2])
    jobs.append({"name": "unit (3.12, 2)", "conclusion": conclusion})
    assert not ci_gate_shards.ready(jobs, [1, 2])
    jobs.append({"name": "unit rerun (3.12, 2)", "conclusion": "success"})
    assert ci_gate_shards.ready(jobs, [1, 2])


def test_workflow_entry_uses_the_current_attempt_and_exact_planned_shards():
    with (
        patch.dict(
            ci_gate_shards.os.environ,
            {
                "GITHUB_REPOSITORY": "org/repo",
                "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "2",
                "SHARDS": "[1, 2, 13]",
            },
        ),
        patch.object(ci_gate_shards, "watch") as watch,
    ):
        ci_gate_shards.main()
    assert watch.call_args.args == ("org/repo", "123", "2", [1, 2, 13])


def test_no_plan_or_an_unrelated_success_never_verifies_a_shard():
    assert not ci_gate_shards.ready([], [])
    assert not ci_gate_shards.ready([{"name": "unit-selected (3.12, 1)", "conclusion": "success"}], [1])
    assert not ci_gate_shards.ready([{"name": "unit (3.11, 1)", "conclusion": "success"}], [1])


@pytest.mark.parametrize("binary", [False, True])
def test_github_reads_paginate_metadata_and_preserve_zip_bytes(binary):
    payload = b"zip bytes" if binary else b'[{"jobs": []}, {"jobs": [{"name": "last"}]}]'
    result = subprocess.CompletedProcess([], 0, stdout=payload)
    with patch.object(ci_gate_shards.subprocess, "run", return_value=result) as run:
        value = ci_gate_shards.api("repos/org/repo/actions", binary=binary)
    assert value == (payload if binary else [{"jobs": []}, {"jobs": [{"name": "last"}]}])
    command = ["gh", "api", "repos/org/repo/actions"]
    if not binary:
        command += ["--paginate", "--slurp"]
    run.assert_called_once_with(command, check=True, capture_output=True, timeout=60, text=False)


def test_metadata_is_decoded_and_paginated_when_binary_is_omitted():
    result = subprocess.CompletedProcess([], 0, stdout=b'[{"artifacts": []}]')
    with patch.object(ci_gate_shards.subprocess, "run", return_value=result) as run:
        assert ci_gate_shards.api("repos/org/repo/actions") == [{"artifacts": []}]
    assert run.call_args.args[0] == ["gh", "api", "repos/org/repo/actions", "--paginate", "--slurp"]


@pytest.mark.parametrize(
    "error",
    [subprocess.CalledProcessError(1, ["gh"]), subprocess.TimeoutExpired(["gh"], 60)],
    ids=["failed", "timed out"],
)
def test_github_failure_is_never_replaced_with_empty_evidence(error):
    with (
        patch.object(ci_gate_shards.subprocess, "run", side_effect=error) as run,
        patch.object(ci_gate_shards.time, "sleep") as sleep,
        pytest.raises(type(error)),
    ):
        ci_gate_shards.api("repos/org/repo/actions")
    assert run.call_count == 4
    assert [call.args for call in sleep.call_args_list] == [(5,), (10,), (20,)]


@pytest.mark.parametrize("binary", [False, True])
def test_a_transient_github_failure_is_retried_with_backoff(binary):
    payload = b"zip bytes" if binary else b'[{"artifacts": []}]'
    results = [
        subprocess.CalledProcessError(1, ["gh"]),
        subprocess.TimeoutExpired(["gh"], 60),
        subprocess.CompletedProcess([], 0, stdout=payload),
    ]
    with (
        patch.object(ci_gate_shards.subprocess, "run", side_effect=results) as run,
        patch.object(ci_gate_shards.time, "sleep") as sleep,
    ):
        value = ci_gate_shards.api("repos/org/repo/actions/runs/123/artifacts?per_page=100", binary=binary)
    assert value == (payload if binary else [{"artifacts": []}])
    assert run.call_count == 3
    assert [call.args for call in sleep.call_args_list] == [(5,), (10,)]


def test_wait_times_out_red_when_a_planned_shard_never_finishes(tmp_path):
    with (
        patch.object(ci_gate_shards.time, "monotonic", side_effect=[0, 0, 1200]),
        patch.object(ci_gate_shards.time, "sleep") as sleep,
        patch.object(ci_gate_shards, "api", return_value=[{"jobs": []}]),
        pytest.raises(RuntimeError, match="timed out"),
    ):
        ci_gate_shards.watch("org/repo", "123", "2", [1])
    sleep.assert_called_once_with(15)


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "skipped"])
def test_nonqueue_verification_rejects_a_terminal_failed_original_without_waiting_for_recovery(tmp_path, conclusion):
    jobs = [{"name": "unit (3.12, 1)", "status": "completed", "conclusion": conclusion}]
    with (
        patch.object(ci_gate_shards, "api", return_value=[{"jobs": jobs}]),
        patch.object(ci_gate_shards.time, "sleep", side_effect=AssertionError("must not wait")),
        pytest.raises(RuntimeError, match="unit \\(3.12, 1\\)"),
    ):
        ci_gate_shards.watch("org/repo", "123", "2", [1], recover=False)


@pytest.mark.parametrize("event", ["pull_request", "push", "workflow_dispatch", "merge_group"])
def test_the_workflow_waits_for_recovery_only_in_the_queue(event):
    with (
        patch.dict(
            ci_gate_shards.os.environ,
            {
                "GITHUB_EVENT_NAME": event,
                "GITHUB_REPOSITORY": "org/repo",
                "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "2",
                "GITHUB_REF": "refs/heads/queue",
                "GITHUB_SHA": "head",
                "SHARDS": "[1]",
            },
        ),
        patch.object(ci_gate_shards, "watch") as watch,
    ):
        ci_gate_shards.main()
    assert watch.call_args.kwargs == {
        "recover": event == "merge_group",
        "queue": ("refs/heads/queue", "head") if event == "merge_group" else None,
    }


def test_nonqueue_prefetch_waits_for_pending_units_ignores_other_jobs_and_accepts_success(tmp_path):
    pending = {"name": "unit (3.12, 1)", "status": "in_progress", "conclusion": None}
    passed = {**pending, "status": "completed", "conclusion": "success"}
    other = {"name": "other", "status": "completed", "conclusion": "failure"}
    pages = [[{"jobs": [pending, other]}], [{"jobs": [passed, other]}]]

    with (
        patch.object(ci_gate_shards, "api", side_effect=pages),
        patch.object(ci_gate_shards.time, "monotonic", side_effect=[0, 0, 0, 1200]),
        patch.object(ci_gate_shards.time, "sleep") as sleep,
    ):
        ci_gate_shards.watch("org/repo", "123", "2", [1], recover=False)
    sleep.assert_called_once_with(15)


def test_existing_watch_callers_keep_queue_recovery_without_a_new_argument(tmp_path):
    jobs = [
        {"name": "unit (3.12, 1)", "status": "completed", "conclusion": "failure"},
        {"name": "unit rerun (3.12, 1)", "status": "completed", "conclusion": "success"},
    ]

    with (
        patch.object(ci_gate_shards, "api", return_value=[{"jobs": jobs}]),
        patch.object(ci_gate_shards.time, "monotonic", side_effect=[0, 0, 1200]),
        patch.object(ci_gate_shards.time, "sleep"),
    ):
        ci_gate_shards.watch("org/repo", "123", "2", [1])


def test_wait_polls_only_the_jobs_page_until_every_shard_passes():
    polls = iter(
        [
            [{"jobs": [{"name": "unit (3.12, 1)", "conclusion": "success"}]}],
            [{"jobs": [{"name": f"unit (3.12, {shard})", "conclusion": "success"} for shard in [1, 2]]}],
        ]
    )
    with (
        patch.object(ci_gate_shards, "api", side_effect=lambda endpoint: next(polls)) as api,
        patch.object(ci_gate_shards.time, "sleep") as sleep,
    ):
        assert ci_gate_shards.watch("org/repo", "123", "2", [1, 2]) is None
    assert [call.args for call in api.call_args_list] == [
        ("repos/org/repo/actions/runs/123/attempts/2/jobs?per_page=100",)
    ] * 2
    sleep.assert_called_once_with(15)


def test_queue_poll_checks_identity_and_full_inventory_without_reading_jobs_twice():
    from scripts import ci_gate_queue

    jobs = [{"name": "unit (3.12, 1)", "conclusion": "success"}]
    with (
        patch.object(ci_gate_shards, "api", return_value=[{"jobs": jobs}]) as api,
        patch.object(ci_gate_queue, "current") as current,
        patch.object(ci_gate_queue, "verify", side_effect=[False, True]) as verify,
        patch.object(ci_gate_queue, "logged_loss", return_value=True) as lost,
        patch.object(ci_gate_shards.time, "sleep") as sleep,
    ):
        ci_gate_shards.watch("org/repo", "123", "2", [1], queue=("refs/heads/queue", "head"))
        assert api.call_count == 2
        assert current.call_args_list == [(("org/repo", "refs/heads/queue", "head"),)] * 3
        assert [call.args for call in verify.call_args_list] == [(jobs, [1])] * 2
        assert all(call.kwargs["preliminary"] is True for call in verify.call_args_list)
        assert verify.call_args.kwargs["lost"](jobs[0]) is True
        lost.assert_called_once_with("org/repo", jobs[0], {})
        sleep.assert_called_once_with(15)


def test_queue_poll_rejects_a_red_inventory_before_successful_units_return():
    from scripts import ci_gate_queue

    with (
        patch.object(ci_gate_shards, "api", return_value=[{"jobs": []}]),
        patch.object(ci_gate_queue, "current"),
        patch.object(ci_gate_queue, "verify", side_effect=ValueError("red group")),
        patch.object(ci_gate_shards.time, "sleep") as sleep,
    ):
        with pytest.raises(ValueError, match="red group"):
            ci_gate_shards.watch("org/repo", "123", "2", [1], queue=("queue", "head"))
        sleep.assert_not_called()


@pytest.mark.parametrize("final", [False, True])
def test_queue_poll_checks_identity_before_listing_and_after_verification(final):
    from scripts import ci_gate_queue

    with (
        patch.object(ci_gate_shards, "api", return_value=[{"jobs": []}]) as api,
        patch.object(
            ci_gate_queue,
            "current",
            side_effect=[None, ValueError("stale group")] if final else ValueError("stale group"),
        ) as current,
        patch.object(ci_gate_queue, "verify", return_value=True) as verify,
        patch.object(ci_gate_shards.time, "sleep") as sleep,
    ):
        with pytest.raises(ValueError, match="stale group"):
            ci_gate_shards.watch("org/repo", "123", "2", [1], queue=("queue", "head"))
        assert api.call_count == int(final)
        assert verify.call_count == int(final)
        assert current.call_count == 1 + int(final)
        sleep.assert_not_called()


@pytest.mark.parametrize("name", ["unit (3.12, 1)", "unit rerun (3.12, 1)"])
def test_runner_queue_wait_never_counts_against_the_watch_deadline(name):
    queued = {"name": name, "status": "queued", "conclusion": None}
    passed = {**queued, "status": "completed", "conclusion": "success"}
    with (
        patch.object(ci_gate_shards, "api", side_effect=[[{"jobs": [queued]}], [{"jobs": [passed]}]]),
        patch.object(ci_gate_shards.time, "monotonic", side_effect=[0, 0, 1300, 1300]),
        patch.object(ci_gate_shards.time, "sleep") as sleep,
    ):
        ci_gate_shards.watch("org/repo", "123", "2", [1])
    sleep.assert_called_once_with(15)
    assert ci_gate_shards.rate_limit.deadline == 2500


@pytest.mark.parametrize("name", ["lint", "unit (3.12, 2)", "unit rerun (3.12, 2)", "unit (3.11, 1)"])
def test_a_queued_job_outside_the_plan_never_extends_the_watch_deadline(name):
    queued = {"name": name, "status": "queued", "conclusion": None}
    with (
        patch.object(ci_gate_shards, "api", return_value=[{"jobs": [queued]}]),
        patch.object(ci_gate_shards.time, "monotonic", side_effect=[0, 0, 1200]),
        patch.object(ci_gate_shards.time, "sleep"),
        pytest.raises(RuntimeError, match="timed out"),
    ):
        ci_gate_shards.watch("org/repo", "123", "2", [1])
    assert ci_gate_shards.rate_limit.deadline == 1200
