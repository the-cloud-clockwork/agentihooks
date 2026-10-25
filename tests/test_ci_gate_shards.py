import io
import subprocess
import zipfile
from unittest.mock import patch

import pytest

from scripts import ci_gate_shards

pytestmark = pytest.mark.unit


def _zip():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("durations.json", '{"test_one": 1}')
    return data.getvalue()


def test_reports_download_during_execution_and_recovery_replaces_stale_evidence(tmp_path):
    initial = {"id": 11, "name": "durations-3.12-1", "expired": False}
    replacement = {**initial, "id": 12}
    calls = []

    def api(endpoint, *, binary=False):
        calls.append(endpoint)
        if endpoint.endswith("/artifacts?per_page=100"):
            return [{"artifacts": [initial if len(calls) == 1 else replacement]}]
        return _zip()

    with patch.object(ci_gate_shards, "api", side_effect=api):
        downloaded = {}
        ci_gate_shards.fetch("org/repo", "123", tmp_path, downloaded)
        assert (tmp_path / initial["name"] / "durations.json").is_file()
        ci_gate_shards.fetch("org/repo", "123", tmp_path, downloaded)
        ci_gate_shards.fetch("org/repo", "123", tmp_path, downloaded)
    assert calls.count("repos/org/repo/actions/artifacts/11/zip") == 1
    assert calls.count("repos/org/repo/actions/artifacts/12/zip") == 1
    assert downloaded == {"durations-3.12-1": 12}


def test_a_report_deleted_before_verification_does_not_count_as_present(tmp_path):
    artifact = {"id": 11, "name": "durations-3.12-1", "expired": False}
    with patch.object(ci_gate_shards, "api", side_effect=[[{"artifacts": [artifact]}], _zip(), [{"artifacts": []}]]):
        downloaded = {}
        ci_gate_shards.fetch("org/repo", "123", tmp_path, downloaded)
        ci_gate_shards.fetch("org/repo", "123", tmp_path, downloaded)
    assert downloaded == {}


def test_recovery_replacement_cannot_keep_a_hash_absent_from_the_new_report(tmp_path):
    folder = tmp_path / "durations-3.12-1"
    folder.mkdir()
    (folder / "durations.sha256").write_text("old hash")
    artifact = {"id": 12, "name": folder.name, "expired": False}
    with patch.object(ci_gate_shards, "api", side_effect=[[{"artifacts": [artifact]}], _zip()]):
        ci_gate_shards.fetch("org/repo", "123", tmp_path, {folder.name: 11})
    assert not (folder / "durations.sha256").exists()


def test_wait_downloads_reports_before_the_last_shard_and_rejects_a_missing_report(tmp_path):
    calls = []
    polls = iter(
        [
            [{"name": "unit (3.12, 1)", "conclusion": "success"}],
            [{"name": f"unit (3.12, {shard})", "conclusion": "success"} for shard in [1, 2]],
        ]
    )

    def api(endpoint, *, binary=False):
        calls.append(endpoint)
        return [{"jobs": next(polls)}]

    def fetch(repository, run, destination, downloaded):
        calls.append("download")
        downloaded.update({f"{kind}-3.12-1": 1 for kind in ["durations", "flakes"]})

    with (
        patch.object(ci_gate_shards, "api", side_effect=api),
        patch.object(ci_gate_shards, "fetch", side_effect=fetch),
        patch.object(ci_gate_shards.time, "sleep"),
        pytest.raises(RuntimeError, match="durations-3.12-2"),
    ):
        ci_gate_shards.watch("org/repo", "123", "2", [1, 2], tmp_path)
    assert calls == [
        "download",
        "repos/org/repo/actions/runs/123/attempts/2/jobs?per_page=100",
        "download",
        "repos/org/repo/actions/runs/123/attempts/2/jobs?per_page=100",
        "download",
    ]


@pytest.mark.parametrize("conclusion", [None, "failure", "cancelled", "skipped"])
def test_incomplete_or_failed_shards_never_finish_verification(conclusion):
    jobs = [{"name": "unit (3.12, 1)", "conclusion": "success"}]
    assert not ci_gate_shards.ready(jobs, [1, 2])
    jobs.append({"name": "unit (3.12, 2)", "conclusion": conclusion})
    assert not ci_gate_shards.ready(jobs, [1, 2])
    jobs.append({"name": "unit-rerun (3.12, 2)", "conclusion": "success"})
    assert ci_gate_shards.ready(jobs, [1, 2])


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
    run.assert_called_once_with(command, check=True, capture_output=True, timeout=60)


def test_github_failure_is_never_replaced_with_empty_evidence():
    with (
        patch.object(ci_gate_shards.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["gh"])),
        pytest.raises(subprocess.CalledProcessError),
    ):
        ci_gate_shards.api("repos/org/repo/actions")


def test_fetch_reads_every_page_and_ignores_foreign_and_expired_artifacts(tmp_path):
    artifacts = [
        {"id": 1, "name": "durations-3.12-2", "expired": False},
        {"id": 2, "name": "flakes-3.12-2", "expired": False},
        {"id": 3, "name": "durations-3.12-3", "expired": True},
        {"id": 4, "name": "durations-3.11-1", "expired": False},
        {"id": 5, "name": "coverage-3.12-1", "expired": False},
    ]
    endpoints = []

    def api(endpoint, *, binary=False):
        endpoints.append(endpoint)
        if not binary:
            return [{"artifacts": artifacts[:1]}, {"artifacts": artifacts[1:]}]
        return _zip()

    with patch.object(ci_gate_shards, "api", side_effect=api):
        downloaded = {}
        ci_gate_shards.fetch("org/repo", "123", tmp_path, downloaded)
    assert downloaded == {"durations-3.12-2": 1, "flakes-3.12-2": 2}
    assert set(endpoints) == {
        "repos/org/repo/actions/runs/123/artifacts?per_page=100",
        "repos/org/repo/actions/artifacts/1/zip",
        "repos/org/repo/actions/artifacts/2/zip",
    }


def test_wait_accepts_complete_recovered_shards_and_refreshes_final_artifacts(tmp_path):
    jobs = [
        {"name": "unit (3.12, 1)", "conclusion": "failure"},
        {"name": "unit-rerun (3.12, 1)", "conclusion": "success"},
        {"name": "unit (3.12, 2)", "conclusion": "success"},
    ]

    def fetch(repository, run, destination, downloaded):
        downloaded.update({f"{kind}-3.12-{shard}": shard for kind in ["durations", "flakes"] for shard in [1, 2]})

    with (
        patch.object(ci_gate_shards, "api", return_value=[{"jobs": jobs[:1]}, {"jobs": jobs[1:]}]),
        patch.object(ci_gate_shards, "fetch", side_effect=fetch) as download,
    ):
        assert ci_gate_shards.watch("org/repo", "123", "2", [1, 2], tmp_path) is None
    assert download.call_count == 2
    assert download.call_args.args[:3] == ("org/repo", "123", tmp_path)


def test_wait_times_out_red_when_a_planned_shard_never_finishes(tmp_path):
    with (
        patch.object(ci_gate_shards.time, "monotonic", side_effect=[0, 0, 1200]),
        patch.object(ci_gate_shards.time, "sleep") as sleep,
        patch.object(ci_gate_shards, "fetch"),
        patch.object(ci_gate_shards, "api", return_value=[{"jobs": []}]),
        pytest.raises(RuntimeError, match="timed out"),
    ):
        ci_gate_shards.watch("org/repo", "123", "2", [1], tmp_path)
    sleep.assert_called_once_with(5)
