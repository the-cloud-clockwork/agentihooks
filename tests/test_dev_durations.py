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


def _restored(folder, merged, version=None):
    folder.mkdir()
    (folder / ".test_durations").write_text(json.dumps(merged))
    if version is not None:
        (folder / ".test_durations-3.12").write_text(json.dumps(version))
    return folder


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
    dev_durations.main(["3.12", str(restored)])
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
        dev_durations.main(["3.12", str(restored)])
