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
                "SHARDS": "[1]",
            },
        ),
        patch.object(ci_gate_shards, "watch") as watch,
    ):
        ci_gate_shards.main()
    assert watch.call_args.kwargs == {"recover": event == "merge_group"}


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


def test_every_planned_report_present_passes(tmp_path):
    for name in ["durations-3.12-1", "flakes-3.12-1", "durations-3.12-2", "flakes-3.12-2"]:
        (tmp_path / name).mkdir()
    assert ci_gate_shards.reports(tmp_path, [1, 2]) is None


def test_a_missing_report_stays_red(tmp_path):
    (tmp_path / "durations-3.12-1").mkdir()
    (tmp_path / "flakes-3.12-2").write_text("not a report folder")
    with pytest.raises(
        RuntimeError, match=r"Missing shard reports: \['durations-3.12-2', 'flakes-3.12-1', 'flakes-3.12-2'\]"
    ):
        ci_gate_shards.reports(tmp_path, [1, 2])


def test_no_downloaded_reports_folder_stays_red(tmp_path):
    with pytest.raises(RuntimeError, match="durations-3.12-1"):
        ci_gate_shards.reports(tmp_path / "absent", [1])


def test_the_reports_command_checks_the_downloaded_folder_without_polling(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ci_gate_shards.sys, "argv", ["ci_gate_shards", "reports"])
    monkeypatch.setenv("SHARDS", "[3]")
    with (
        patch.object(ci_gate_shards, "watch", side_effect=AssertionError("must not poll")),
        pytest.raises(RuntimeError, match=r"\['durations-3.12-3', 'flakes-3.12-3'\]"),
    ):
        ci_gate_shards.main()


def test_the_reports_command_passes_on_the_downloaded_reports_folder(tmp_path, monkeypatch):
    for kind in ["durations", "flakes"]:
        (tmp_path / "shard-reports" / f"{kind}-3.12-3").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ci_gate_shards.sys, "argv", ["ci_gate_shards", "reports"])
    monkeypatch.setenv("SHARDS", "[3]")
    with patch.object(ci_gate_shards, "watch", side_effect=AssertionError("must not poll")):
        assert ci_gate_shards.main() is None
