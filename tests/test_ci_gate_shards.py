import io
import json
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


@pytest.mark.parametrize("conclusion", [None, "failure", "cancelled", "skipped"])
def test_incomplete_or_failed_shards_never_finish_verification(conclusion):
    jobs = [{"name": "unit (3.12, 1)", "conclusion": "success"}]
    assert not ci_gate_shards.ready(jobs, [1, 2])
    jobs.append({"name": "unit (3.12, 2)", "conclusion": conclusion})
    assert not ci_gate_shards.ready(jobs, [1, 2])
    jobs.append({"name": "unit-rerun (3.12, 2)", "conclusion": "success"})
    assert ci_gate_shards.ready(jobs, [1, 2])
