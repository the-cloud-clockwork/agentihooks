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
    for version in ("3.11", "3.12"):
        _shard(samples, "11", version, 1, {"t.py::a@g": 1.0})
        _shard(samples, "11", version, 2, {"t.py::old@g": 1.0})
        _shard(samples, "12", version, 1, {"t.py::a@g": 1.0})

    assert _refresh(monkeypatch, samples, ["t.py::a", "t.py::b"]) == 1
    assert capsys.readouterr().out == (
        "::error::refresh durations: 1 of 2 tests have no stored duration;"
        " run 12 kept no samples from durations-3.11-2, durations-3.12-2\n"
    )


def test_a_run_that_lost_a_whole_version_names_its_shards(tmp_path, monkeypatch, capsys):
    samples = tmp_path / "samples"
    for shard in (1, 2):
        _shard(samples, "11", "3.11", shard, {"t.py::a@g": 1.0})
        _shard(samples, "11", "3.12", shard, {"t.py::a@g": 1.0})
        _shard(samples, "12", "3.12", shard, {"t.py::a@g": 1.0})

    assert _refresh(monkeypatch, samples, ["t.py::a"]) == 1
    assert capsys.readouterr().out == (
        "::error::refresh durations: run 12 kept no durations for Python 3.11;"
        " run 12 kept no samples from durations-3.11-1, durations-3.11-2\n"
    )


def test_a_malformed_sample_file_is_named(tmp_path, monkeypatch, capsys):
    samples = tmp_path / "samples"
    _shard(samples, "12", "3.11", 1, "{not json")
    _shard(samples, "12", "3.12", 1, {"t.py::a@g": 1.0})

    assert _refresh(monkeypatch, samples, ["t.py::a"]) == 1
    line = capsys.readouterr().out
    assert line.startswith(
        f"::error::refresh durations: malformed sample file {samples}/12/durations-3.11-1/durations.json: "
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


def test_a_usage_exit_and_a_success_keep_their_own_outcome(monkeypatch, capsys):
    monkeypatch.setattr(refresh_durations, "main", lambda argv: None)
    assert refresh_durations.cli(["--ci-run", "3"]) is None
    monkeypatch.setattr(refresh_durations, "main", lambda argv: refresh_durations.parser().parse_args(argv))
    with pytest.raises(SystemExit) as stopped:
        refresh_durations.cli(["--unknown"])
    assert stopped.value.code == 2
    assert "::error::" not in capsys.readouterr().out
