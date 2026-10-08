import hashlib
import json

import pytest

from tests import dev_durations


@pytest.fixture(autouse=True)
def _collected_suite(monkeypatch):
    monkeypatch.setattr(dev_durations, "collected_tests", lambda root: ["t.py::a"])


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


RUN = "1970-01-01T01:00:00Z"


def _restored(folder, merged, version=None, saved_at=1000):
    folder.mkdir()
    (folder / ".test_durations").write_text(json.dumps(merged))
    if version is not None:
        (folder / ".test_durations-3.12").write_text(json.dumps(version))
    if saved_at is not None:
        (folder / "saved-at").write_text(f"{saved_at}\n")
    return folder


def _shard(root, restored, run_time=RUN):
    root.mkdir(exist_ok=True)
    (root / ".test_durations").write_text('{"t.py::a": 1.0}')
    dev_durations._ROOT = root
    dev_durations.main(["3.12", str(restored), "--run-time", run_time, "--hash", str(root / "durations.sha256")])
    return (root / "durations.sha256").read_text().strip()


def test_two_shards_of_one_run_refuse_a_cache_saved_after_the_run_began_and_report_one_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_durations, "_ROOT", tmp_path)
    late = _restored(tmp_path / "late", {"t.py::a": 9.0}, saved_at=3500)
    first = _shard(tmp_path / "first", tmp_path / "missing")
    second = _shard(tmp_path / "second", late)
    assert first == second
    assert json.loads((tmp_path / "second" / ".test_durations").read_text()) == {"t.py::a": 1.0}


def test_two_shards_of_one_run_adopt_a_cache_saved_before_the_run_began_and_report_one_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_durations, "_ROOT", tmp_path)
    early = _restored(tmp_path / "early", {"t.py::a": 9.0}, saved_at=3000)
    first = _shard(tmp_path / "first", early)
    second = _shard(tmp_path / "second", early)
    assert first == second
    assert first == hashlib.sha256((tmp_path / "first" / ".test_durations").read_bytes()).hexdigest()
    assert json.loads((tmp_path / "first" / ".test_durations").read_text()) == {"t.py::a": 9.0}


@pytest.mark.parametrize(("saved_at", "run_time"), [(None, RUN), (1000, "")])
def test_a_cache_with_no_save_time_or_a_run_with_no_event_time_keeps_the_committed_durations(
    tmp_path, monkeypatch, saved_at, run_time
):
    monkeypatch.setattr(dev_durations, "_ROOT", tmp_path)
    restored = _restored(tmp_path / "restored", {"t.py::a": 9.0}, saved_at=saved_at)
    _shard(tmp_path / "shard", restored, run_time)
    assert json.loads((tmp_path / "shard" / ".test_durations").read_text()) == {"t.py::a": 1.0}


@pytest.mark.parametrize(
    ("saved_at", "run_time", "adopted"),
    [
        (3300, RUN, True),
        (3301, RUN, False),
        (3300, "1970-01-01T02:00:00+01:00", True),
        (3301, "1970-01-01T02:00:00+01:00", False),
        (3300, "3600", True),
        (3301, "3600", False),
    ],
)
def test_a_cache_is_adopted_only_when_saved_five_minutes_before_the_event_time(tmp_path, saved_at, run_time, adopted):
    restored = _restored(tmp_path / "restored", {"t.py::a": 9.0}, saved_at=saved_at)
    assert dev_durations.saved_before(restored, run_time) is adopted


def test_main_keeps_the_committed_version_file_on_a_cache_miss(tmp_path, monkeypatch):
    (tmp_path / ".test_durations-3.12").write_text('{"t.py::a": 1.0}')
    monkeypatch.setattr(dev_durations, "_ROOT", tmp_path)
    dev_durations.main(["3.12", str(tmp_path / "missing")])
    assert json.loads((tmp_path / ".test_durations").read_text()) == {"t.py::a": 1.0}


def test_main_stays_green_on_a_cache_miss_with_incomplete_committed_durations(tmp_path, monkeypatch, capsys):
    saved = '{"t.py::b": 1.0}\n'
    (tmp_path / ".test_durations").write_text(saved)
    (tmp_path / ".test_durations-3.12").write_text(saved)
    monkeypatch.setattr(dev_durations, "_ROOT", tmp_path)
    dev_durations.main(["3.12", str(tmp_path / "missing")])
    assert json.loads((tmp_path / ".test_durations").read_text()) == {"t.py::a": 1.0, "t.py::b": 1.0}
    assert (tmp_path / ".test_durations-3.12").read_text() == saved
    assert "1 of 1 tests have no stored duration" in capsys.readouterr().out


def test_main_adopts_the_restored_dev_durations(tmp_path, monkeypatch):
    restored = _restored(tmp_path / "restored", {"t.py::a": 2.0, "t.py::b": 4.0}, {"t.py::a": 3.0})
    monkeypatch.setattr(dev_durations, "_ROOT", tmp_path)
    dev_durations.main(["3.12", str(restored), "--run-time", RUN])
    assert json.loads((tmp_path / ".test_durations").read_text()) == {"t.py::a": 3.0, "t.py::b": 4.0}


def test_main_never_calls_github(tmp_path, monkeypatch):
    tools = tmp_path / "bin"
    tools.mkdir()
    called = tmp_path / "gh-called"
    (tools / "gh").write_text(f"#!/bin/sh\ntouch {called}\nexit 1\n")
    (tools / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", str(tools))
    (tmp_path / ".test_durations").write_text('{"t.py::a": 1.0}')
    restored = _restored(tmp_path / "restored", {"t.py::a": 2.0})
    monkeypatch.setattr(dev_durations, "_ROOT", tmp_path)
    for folder in (restored, tmp_path / "missing"):
        dev_durations.main(["3.12", str(folder)])
    assert not called.exists()


def test_main_fails_the_shard_when_the_restored_durations_cannot_be_adopted(tmp_path, monkeypatch):
    restored = _restored(tmp_path / "restored", {}, {})
    monkeypatch.setattr(dev_durations, "_ROOT", tmp_path)
    with pytest.raises(ValueError):
        dev_durations.main(["3.12", str(restored), "--run-time", RUN])
