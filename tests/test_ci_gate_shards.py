import json
import math
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

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
        command += ["--paginate", "--slurp", "--include"]
    run.assert_called_once_with(command, check=True, capture_output=True, timeout=60, text=False)


def test_metadata_is_decoded_and_paginated_when_binary_is_omitted():
    result = subprocess.CompletedProcess([], 0, stdout=b'[{"artifacts": []}]')
    with patch.object(ci_gate_shards.subprocess, "run", return_value=result) as run:
        assert ci_gate_shards.api("repos/org/repo/actions") == [{"artifacts": []}]
    assert run.call_args.args[0] == ["gh", "api", "repos/org/repo/actions", "--paginate", "--slurp", "--include"]


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


def test_wait_times_out_red_when_a_planned_shard_never_finishes(tmp_path: Path) -> None:
    with (
        patch.object(ci_gate_shards.time, "monotonic", side_effect=[0, 0, 1200]),
        patch.object(ci_gate_shards.time, "sleep") as sleep,
        patch.object(ci_gate_shards, "read_jobs", return_value=[]),
        pytest.raises(RuntimeError, match="timed out"),
    ):
        ci_gate_shards.watch("org/repo", "123", "2", [1])
    sleep.assert_called_once_with(15)


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "skipped"])
def test_nonqueue_verification_rejects_a_terminal_failed_original_without_waiting_for_recovery(
    tmp_path: Path, conclusion: str
) -> None:
    jobs = [{"name": "unit (3.12, 1)", "status": "completed", "conclusion": conclusion}]
    with (
        patch.object(ci_gate_shards, "read_jobs", return_value=jobs),
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
                "SHARDS": "[1]",
            },
        ),
        patch.object(ci_gate_shards, "watch") as watch,
    ):
        ci_gate_shards.main()
    assert watch.call_args.kwargs == {"recover": event == "merge_group"}


def test_nonqueue_prefetch_waits_for_pending_units_ignores_other_jobs_and_accepts_success(tmp_path: Path) -> None:
    pending = {"name": "unit (3.12, 1)", "status": "in_progress", "conclusion": None}
    passed = {**pending, "status": "completed", "conclusion": "success"}
    other = {"name": "other", "status": "completed", "conclusion": "failure"}
    pages = [[pending, other], [passed, other]]

    with (
        patch.object(ci_gate_shards, "read_jobs", side_effect=pages),
        patch.object(ci_gate_shards.time, "monotonic", side_effect=[0, 0, 0, 1200]),
        patch.object(ci_gate_shards.time, "sleep") as sleep,
    ):
        ci_gate_shards.watch("org/repo", "123", "2", [1], recover=False)
    sleep.assert_called_once_with(15)


def test_existing_watch_callers_keep_queue_recovery_without_a_new_argument(tmp_path: Path) -> None:
    jobs = [
        {"name": "unit (3.12, 1)", "status": "completed", "conclusion": "failure"},
        {"name": "unit rerun (3.12, 1)", "status": "completed", "conclusion": "success"},
    ]

    with (
        patch.object(ci_gate_shards, "read_jobs", return_value=jobs),
        patch.object(ci_gate_shards.time, "monotonic", side_effect=[0, 0, 1200]),
        patch.object(ci_gate_shards.time, "sleep"),
    ):
        ci_gate_shards.watch("org/repo", "123", "2", [1])


@pytest.mark.feature("shard-verifier-small-job-pages")
def test_wait_polls_only_the_jobs_page_until_every_shard_passes() -> None:
    polls = iter(
        [
            [{"name": "unit (3.12, 1)", "conclusion": "success"}],
            [{"name": f"unit (3.12, {shard})", "conclusion": "success"} for shard in [1, 2]],
        ]
    )
    with (
        patch.object(ci_gate_shards, "read_jobs", side_effect=lambda endpoint, size: next(polls)) as read_jobs,
        patch.object(ci_gate_shards.time, "sleep") as sleep,
    ):
        assert ci_gate_shards.watch("org/repo", "123", "2", [1, 2]) is None
    assert [call.args for call in read_jobs.call_args_list] == [
        ("repos/org/repo/actions/runs/123/attempts/2/jobs", 10)
    ] * 2
    sleep.assert_called_once_with(15)


@pytest.mark.parametrize("name", ["unit (3.12, 1)", "unit rerun (3.12, 1)"])
def test_a_queued_planned_job_never_extends_the_watch_deadline(name: str) -> None:
    queued = {"name": name, "status": "queued", "conclusion": None}
    with (
        patch.object(ci_gate_shards, "read_jobs", return_value=[queued]),
        patch.object(ci_gate_shards.time, "monotonic", side_effect=[0, 0, 200, 300]),
        patch.object(ci_gate_shards.time, "sleep") as sleep,
        pytest.raises(RuntimeError, match="timed out after 300 s and 2 polls"),
    ):
        ci_gate_shards.watch("org/repo", "123", "2", [1])
    assert [call.args for call in sleep.call_args_list] == [(15,), (15,)]
    assert ci_gate_shards.rate_limit.deadline == 300


@pytest.mark.parametrize("name", ["lint", "unit (3.12, 2)", "unit rerun (3.12, 2)", "unit (3.11, 1)"])
def test_a_queued_job_outside_the_plan_never_extends_the_watch_deadline(name: str) -> None:
    queued = {"name": name, "status": "queued", "conclusion": None}
    with (
        patch.object(ci_gate_shards, "read_jobs", return_value=[queued]),
        patch.object(ci_gate_shards.time, "monotonic", side_effect=[0, 0, 200, 300]),
        patch.object(ci_gate_shards.time, "sleep"),
        pytest.raises(RuntimeError, match="timed out after 300 s and 2 polls"),
    ):
        ci_gate_shards.watch("org/repo", "123", "2", [1])
    assert ci_gate_shards.rate_limit.deadline == 300


def test_a_wait_that_never_polls_names_every_planned_shard() -> None:
    with (
        patch.object(ci_gate_shards, "read_jobs", side_effect=AssertionError("must not poll")),
        patch.object(ci_gate_shards.time, "monotonic", side_effect=[0, 300]),
        pytest.raises(RuntimeError) as raised,
    ):
        ci_gate_shards.watch("org/repo", "123", "2", [1])
    assert str(raised.value) == (
        "Shard verification timed out after 300 s and 0 polls before every expected shard succeeded;"
        " still waiting on every planned shard"
    )


def test_waiting_names_each_missing_shard_by_its_unit_and_rerun_state():
    jobs = [
        {"name": "unit (3.12, 1)", "status": "completed", "conclusion": "success"},
        {"name": "unit (3.12, 2)", "status": "in_progress", "conclusion": None},
        {"name": "unit (3.12, 3)", "status": "completed", "conclusion": "failure"},
        {"name": "unit rerun (3.12, 3)", "status": "queued", "conclusion": None},
        {"name": "unit (3.12, 5)", "status": "completed", "conclusion": "failure"},
        {"name": "unit rerun (3.12, 5)", "status": "completed", "conclusion": "success"},
        {"name": "unit (3.12, 6)"},
        {"name": "unit (3.12, 7)", "status": "queued", "conclusion": None},
        {"name": "unit (3.12, 7)", "status": "completed", "conclusion": "success"},
        {"name": "unit (3.12, 8)", "status": "completed", "conclusion": "cancelled"},
        {"name": "unit (3.12, 8)", "status": "queued"},
        {"name": "unit (3.11, 4)", "status": "completed", "conclusion": "success"},
        {"name": "unit (3.12, 9)", "status": "completed", "conclusion": "failure"},
        {"name": "unit rerun (3.12, 9)", "status": "completed", "conclusion": "failure"},
        {"name": "unit rerun (3.12, 9)", "status": "queued", "conclusion": None},
    ]
    assert ci_gate_shards.waiting(jobs, [1, 2, 3, 4, 5, 6, 7, 8, 9]) == [
        "shard 2 unit in_progress",
        "shard 3 unit failure, rerun queued",
        "shard 4 unit not listed",
        "shard 6 unit unknown",
        "shard 8 unit cancelled, queued",
        "shard 9 unit failure, rerun failure, queued",
    ]
    assert ci_gate_shards.waiting([], []) == []


def test_watch_logs_one_line_per_poll_with_what_it_still_waits_for(capsys: pytest.CaptureFixture[str]) -> None:
    pending = {"name": "unit (3.12, 2)", "status": "in_progress", "conclusion": None}
    first = [{"name": "unit (3.12, 1)", "status": "completed", "conclusion": "success"}, pending]
    second = [
        first[0],
        {**pending, "status": "completed", "conclusion": "success"},
        {"name": "unit (3.12, 3)", "status": "completed", "conclusion": "success"},
    ]
    with (
        patch.object(ci_gate_shards, "read_jobs", side_effect=[first, second]),
        patch.object(ci_gate_shards.time, "monotonic", side_effect=[100, 100, 115]),
        patch.object(ci_gate_shards.time, "sleep"),
    ):
        ci_gate_shards.watch("org/repo", "123", "2", [1, 2, 3])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "Poll 1 at 0 s: 1 of 3 planned shards passed; waiting on shard 2 unit in_progress; shard 3 unit not listed",
        "Poll 2 at 15 s: 3 of 3 planned shards passed; waiting on nothing",
    ]


def test_timeout_names_every_shard_still_missing_at_the_last_poll() -> None:
    jobs = [{"name": "unit (3.12, 1)", "status": "in_progress", "conclusion": None}]
    with (
        patch.object(ci_gate_shards, "read_jobs", return_value=jobs),
        patch.object(ci_gate_shards.time, "monotonic", side_effect=[0, 0, 299, 300]),
        patch.object(ci_gate_shards.time, "sleep"),
        pytest.raises(RuntimeError) as raised,
    ):
        ci_gate_shards.watch("org/repo", "123", "2", [1, 2])
    assert str(raised.value) == (
        "Shard verification timed out after 300 s and 2 polls before every expected shard succeeded;"
        " still waiting on shard 1 unit in_progress; shard 2 unit not listed"
    )
    assert ci_gate_shards.rate_limit.deadline == 300


def rerun_attempt_jobs() -> list[dict[str, Any]]:
    others = [
        {"name": f"mutation-shard ({index})", "status": "completed", "conclusion": "success"} for index in range(82)
    ]
    units = [
        {"name": f"unit (3.12, {shard})", "status": "completed", "conclusion": "success"} for shard in range(1, 21)
    ]
    return others + units


def github_attempt_jobs(
    jobs: list[dict[str, Any]], requests: list[str], largest_page: int
) -> Callable[[list[str], float, bool], subprocess.CompletedProcess[Any]]:
    def answer(args: list[str], timeout: float, text: bool) -> subprocess.CompletedProcess[Any]:
        requests.append(args[0])
        query = parse_qs(urlsplit(args[0]).query)
        size = int(query["per_page"][0])
        if size > largest_page:
            raise subprocess.CalledProcessError(1, ["gh", "api", *args], stderr="gh: Server Error (HTTP 502)")
        first = int(query.get("page", ["1"])[0])
        last = math.ceil(len(jobs) / size) if "--paginate" in args else first
        pages = [
            {"total_count": len(jobs), "jobs": jobs[(page - 1) * size : page * size]} for page in range(first, last + 1)
        ]
        body = json.dumps(pages if "--slurp" in args else pages[0])
        return subprocess.CompletedProcess(["gh", "api", *args], 0, stdout=body if text else body.encode())

    return answer


@pytest.mark.feature("shard-verifier-small-job-pages")
def test_watch_passes_every_planned_shard_although_a_full_jobs_page_answers_bad_gateway() -> None:
    requests: list[str] = []
    with (
        patch.object(ci_gate_shards.rate_limit, "_run", github_attempt_jobs(rerun_attempt_jobs(), requests, 25)),
        patch.object(ci_gate_shards.time, "sleep"),
    ):
        assert ci_gate_shards.watch("org/repo", "123", "3", list(range(1, 21))) is None


@pytest.mark.feature("shard-verifier-small-job-pages")
def test_watch_reads_attempt_jobs_ten_per_page_and_never_a_hundred() -> None:
    requests: list[str] = []
    with (
        patch.object(ci_gate_shards.rate_limit, "_run", github_attempt_jobs(rerun_attempt_jobs(), requests, 100)),
        patch.object(ci_gate_shards.time, "sleep"),
    ):
        ci_gate_shards.watch("org/repo", "123", "3", list(range(1, 21)))
    assert requests == [
        f"repos/org/repo/actions/runs/123/attempts/3/jobs?per_page=10&page={page}" for page in range(1, 12)
    ]
