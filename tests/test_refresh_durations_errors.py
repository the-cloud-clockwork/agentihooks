import json
import runpy
import sys

import pytest

from tests import duration_coverage, refresh_durations

pytestmark = pytest.mark.unit


def _shard(samples, run, version, shard, durations):
    path = samples / run / f"durations-{version}-{shard}"
    path.mkdir(parents=True)
    (path / "durations.json").write_text(durations if isinstance(durations, str) else json.dumps(durations))


def _refresh(monkeypatch, samples, collected):
    monkeypatch.setattr(duration_coverage, "collected_tests", lambda root: collected)
    monkeypatch.setattr(sys, "argv", ["refresh_durations", "--samples", str(samples), "--ci-run", "12", "--ci", "5"])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("tests.refresh_durations", run_name="__main__")
    return stopped.value.code


def test_a_refresh_missing_one_shard_names_it_in_one_error_line(tmp_path, monkeypatch, capsys):
    samples = tmp_path / "samples"
    for version in refresh_durations.VERSIONS:
        _shard(samples, "11", version, 2, {"t.py::old@g": 1.0})
        for shard in range(1, refresh_durations.SHARDS + 1):
            if shard != 2:
                _shard(samples, "12", version, shard, {"t.py::a@g": 1.0})

    assert _refresh(monkeypatch, samples, ["t.py::a", "t.py::b"]) == 1
    assert capsys.readouterr().out == (
        "::error::refresh durations: 1 of 2 tests have no stored duration;"
        " run 12 kept no samples from durations-3.12-2\n"
    )


def test_a_run_that_lost_every_shard_names_them_all(tmp_path, monkeypatch, capsys):
    samples = tmp_path / "samples"
    samples.mkdir()

    assert _refresh(monkeypatch, samples, ["t.py::a"]) == 1
    lost = ", ".join(f"durations-3.12-{shard}" for shard in range(1, refresh_durations.SHARDS + 1))
    assert capsys.readouterr().out == (
        f"::error::refresh durations: run 12 kept no durations for Python 3.12; run 12 kept no samples from {lost}\n"
    )


def test_a_malformed_sample_file_is_named(tmp_path, monkeypatch, capsys):
    samples = tmp_path / "samples"
    _shard(samples, "12", "3.12", 1, "{not json")
    _shard(samples, "12", "3.12", 2, {"t.py::a@g": 1.0})

    assert _refresh(monkeypatch, samples, ["t.py::a"]) == 1
    line = capsys.readouterr().out
    assert line.startswith(
        f"::error::refresh durations: malformed sample file {samples}/12/durations-3.12-1/durations.json: "
    )
    assert line.count("\n") == 1


@pytest.mark.parametrize(
    ("error", "line"),
    [
        (RuntimeError(), "RuntimeError"),
        (RuntimeError("collection\nfailed"), "collection failed"),
        (SystemExit("run 3 kept no durations"), "run 3 kept no durations"),
    ],
)
def test_every_failure_prints_one_named_line(monkeypatch, capsys, error, line):
    def fail(argv):
        raise error

    monkeypatch.setattr(refresh_durations, "main", fail)
    with pytest.raises(SystemExit) as stopped:
        refresh_durations.cli(["--ci-run", "3"])
    assert stopped.value.code == 1
    assert capsys.readouterr().out == f"::error::refresh durations: {line}\n"


def test_a_success_returns_and_a_usage_exit_keeps_its_code_with_a_named_line(monkeypatch, capsys):
    monkeypatch.setattr(refresh_durations, "main", lambda argv: None)
    assert refresh_durations.cli(["--ci-run", "3"]) is None
    assert capsys.readouterr().out == ""
    with pytest.raises(SystemExit) as stopped:
        refresh_durations.cli(["--unknown"])
    assert stopped.value.code == 2
    assert capsys.readouterr().out == "::error::refresh durations: invalid arguments --unknown\n"
