import io
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
