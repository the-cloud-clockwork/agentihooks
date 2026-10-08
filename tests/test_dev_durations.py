import json
import subprocess

import pytest

from tests import dev_durations


def _artifact(run, created, branch="dev", expired=False):
    return {"expired": expired, "created_at": created, "workflow_run": {"id": run, "head_branch": branch}}


@pytest.fixture(autouse=True)
def _collected_suite(monkeypatch):
    monkeypatch.setattr(dev_durations, "collected_tests", lambda root: ["t.py::a"])


def _gh(listing, created):
    calls = []

    def gh(args):
        calls.append(args)
        if "actions/runs/" in args[1]:
            return created + "\n"
        jq = args[args.index("--jq") + 1]
        return subprocess.run(
            ["jq", "-r", jq], input=json.dumps(listing), capture_output=True, text=True, check=True
        ).stdout

    return gh, calls


def test_source_run_ignores_artifacts_published_after_this_run_was_created():
    listing = {
        "artifacts": [
            _artifact(5, "2026-10-08T09:00:00Z"),
            _artifact(9, "2026-10-08T09:30:00Z"),
            _artifact(11, "2026-10-08T10:30:00Z"),
            _artifact(8, "2026-10-08T09:45:00Z", branch="feature"),
            _artifact(7, "2026-10-08T09:50:00Z", expired=True),
        ]
    }
    gh, calls = _gh(listing, "2026-10-08T10:00:00Z")
    assert dev_durations.source_run("42", gh) == "9"
    assert calls[0][1] == "repos/{owner}/{repo}/actions/runs/42"
    assert calls[1][:2] == ["api", "--paginate"]


def test_source_run_is_empty_without_an_earlier_dev_artifact():
    gh, _ = _gh({"artifacts": [_artifact(11, "2026-10-08T10:30:00Z")]}, "2026-10-08T10:00:00Z")
    assert dev_durations.source_run("42", gh) == ""


def test_adopt_fills_the_tests_the_version_file_lacks_from_the_merged_file(tmp_path):
    (tmp_path / ".test_durations").write_text(json.dumps({"t.py::a": 2.0, "t.py::b": 4.0}))
    (tmp_path / ".test_durations-3.12").write_text(json.dumps({"t.py::a": 3.0}))
    assert dev_durations.adopt(tmp_path, "3.12") == {"t.py::a": 3.0, "t.py::b": 4.0}


def test_adopt_takes_the_merged_file_of_an_artifact_without_a_version_file(tmp_path):
    (tmp_path / ".test_durations").write_text(json.dumps({"t.py::a": 2.0}))
    assert dev_durations.adopt(tmp_path, "3.12") == {"t.py::a": 2.0}


@pytest.mark.parametrize("bad", [{}, {"t.py::a": -1.0}, {"t.py::a": float("inf")}, {"t.py::a": "1"}, []])
def test_adopt_rejects_a_file_that_is_not_durations(tmp_path, bad):
    (tmp_path / ".test_durations").write_text(json.dumps({"t.py::a": 2.0}))
    (tmp_path / ".test_durations-3.12").write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        dev_durations.adopt(tmp_path, "3.12")


def test_main_keeps_the_committed_version_file_without_an_earlier_dev_artifact(tmp_path, monkeypatch):
    (tmp_path / ".test_durations-3.12").write_text('{"t.py::a": 1.0}')
    monkeypatch.setattr(dev_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(dev_durations, "source_run", lambda run_id: "")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    dev_durations.main(["3.12"])
    assert json.loads((tmp_path / ".test_durations").read_text()) == {"t.py::a": 1.0}


def test_main_adopts_the_downloaded_artifact_of_the_chosen_run(tmp_path, monkeypatch):
    downloads = []

    def download(run, folder):
        downloads.append(run)
        (folder / ".test_durations").write_text(json.dumps({"t.py::a": 2.0, "t.py::b": 4.0}))
        (folder / ".test_durations-3.12").write_text(json.dumps({"t.py::a": 3.0}))

    monkeypatch.setattr(dev_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(dev_durations, "source_run", lambda run_id: "9")
    monkeypatch.setattr(dev_durations, "download", download)
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    dev_durations.main(["3.12"])
    assert downloads == ["9"]
    assert json.loads((tmp_path / ".test_durations").read_text()) == {"t.py::a": 3.0, "t.py::b": 4.0}


def test_main_fails_the_shard_when_the_chosen_artifact_cannot_be_adopted(tmp_path, monkeypatch):
    def download(run, folder):
        (folder / ".test_durations").write_text("{}")
        (folder / ".test_durations-3.12").write_text("{}")

    monkeypatch.setattr(dev_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(dev_durations, "source_run", lambda run_id: "9")
    monkeypatch.setattr(dev_durations, "download", download)
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    with pytest.raises(ValueError):
        dev_durations.main(["3.12"])
