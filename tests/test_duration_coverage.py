import json
import subprocess
from pathlib import Path

import pytest

from tests import dev_durations, refresh_durations


def _suite(root, count=100):
    (root / "tests").mkdir()
    (root / "tests/test_probe.py").write_text("\n".join(f"def test_{i}(): pass" for i in range(count)))
    return {f"tests/test_probe.py::test_{i}": 1.0 for i in range(count)}


@pytest.mark.parametrize("missing", [10, 11])
def test_download_checks_coverage_before_replacing_complete_durations(tmp_path, monkeypatch, capsys, missing):
    complete = _suite(tmp_path)
    committed = json.dumps(complete) + "\n"
    (tmp_path / ".test_durations-3.12").write_text(committed)
    candidate = dict(list(complete.items())[:-missing])
    restored = tmp_path / "restored"
    restored.mkdir()
    (restored / ".test_durations").write_text(json.dumps(candidate))

    monkeypatch.setattr(dev_durations, "_ROOT", tmp_path)
    monkeypatch.setenv("PYTEST_ADDOPTS", "--shard 1/4")
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    dev_durations.main(["3.12", str(restored)])
    if missing == 10:
        assert json.loads((tmp_path / ".test_durations").read_text()) == candidate
    else:
        assert (tmp_path / ".test_durations").read_text() == committed
        assert "11 of 100 tests have no stored duration" in capsys.readouterr().out


@pytest.mark.parametrize("incomplete", ["3.11", "3.12", "*"])
def test_publication_refuses_incomplete_maps_before_writing_any_file(tmp_path, monkeypatch, incomplete):
    complete = _suite(tmp_path)
    saved = {}
    for version in ("", "-3.11", "-3.12"):
        path = tmp_path / f".test_durations{version}"
        path.write_text(json.dumps(complete))
        saved[path] = path.read_bytes()
    monkeypatch.setattr(refresh_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(refresh_durations, "ci_download", lambda runs, folder: None)
    monkeypatch.setattr(
        refresh_durations,
        "ci_medians",
        lambda folder, version, source, collected: (
            dict(list(complete.items())[:89]) if version == incomplete else complete
        ),
    )
    with pytest.raises(ValueError, match="11 of 100 tests have no stored duration"):
        refresh_durations.main(["--ci-run", "42"])
    assert {path: path.read_bytes() for path in saved} == saved


@pytest.mark.parametrize("seam", ["publication", "download"])
def test_recorded_incomplete_durations_are_refused(tmp_path, monkeypatch, capsys, seam):
    import gzip

    record = json.loads(
        gzip.decompress((Path(__file__).parent / "fixtures/durations-37763891713.json.gz").read_bytes())
    )
    collected = record["collected"]
    incomplete = record["durations"]
    assert len(collected) == 12891
    assert len(incomplete) == 11451
    assert sum(node not in incomplete for node in collected) == 1440
    complete = dict.fromkeys(collected, 1.0)
    saved = json.dumps(complete) + "\n"
    for suffix in ("", "-3.11", "-3.12"):
        (tmp_path / f".test_durations{suffix}").write_text(saved)
    if seam == "publication":
        monkeypatch.setattr(refresh_durations, "_ROOT", tmp_path)
        monkeypatch.setattr(refresh_durations, "collected_tests", lambda root: collected)
        monkeypatch.setattr(refresh_durations, "ci_download", lambda runs, folder: None)
        monkeypatch.setattr(
            refresh_durations,
            "ci_medians",
            lambda folder, version, source, collected: incomplete if version == "3.12" else complete,
        )
        with pytest.raises(ValueError, match="1440 of 12891 tests have no stored duration"):
            refresh_durations.main(["--ci-run", str(record["producer_run"])])
    else:
        restored = tmp_path / "restored"
        restored.mkdir()
        (restored / ".test_durations").write_text(json.dumps(incomplete))
        monkeypatch.setattr(dev_durations, "_ROOT", tmp_path)
        monkeypatch.setattr(dev_durations, "collected_tests", lambda root: collected)
        dev_durations.main(["3.12", str(restored)])
        assert "1440 of 12891 tests have no stored duration" in capsys.readouterr().out
    for suffix in ("", "-3.11", "-3.12"):
        assert (tmp_path / f".test_durations{suffix}").read_text() == saved


def test_download_keeps_complete_merged_fallback_when_committed_version_is_incomplete(tmp_path, monkeypatch):
    complete = _suite(tmp_path)
    saved = json.dumps(complete) + "\n"
    (tmp_path / ".test_durations").write_text(saved)
    (tmp_path / ".test_durations-3.12").write_text(json.dumps(dict(list(complete.items())[:89])))
    monkeypatch.setattr(dev_durations, "_ROOT", tmp_path)
    dev_durations.main(["3.12", str(tmp_path / "missing")])
    assert (tmp_path / ".test_durations").read_text() == saved


@pytest.mark.parametrize("seam", ["publication", "download"])
def test_failed_collection_never_replaces_durations(tmp_path, monkeypatch, seam):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_broken.py").write_text("raise RuntimeError('collection failed')")
    saved = '{"tests/test_probe.py::test_0": 1.0}\n'
    (tmp_path / ".test_durations").write_text(saved)
    if seam == "download":
        monkeypatch.setattr(dev_durations, "_ROOT", tmp_path)
        with pytest.raises(RuntimeError, match="collection failed"):
            dev_durations.main(["3.12", str(tmp_path / "missing")])
    else:
        monkeypatch.setattr(refresh_durations, "_ROOT", tmp_path)
        monkeypatch.setattr(refresh_durations, "ci_download", lambda runs, folder: None)
        monkeypatch.setattr(refresh_durations, "ci_medians", lambda folder, version, source, collected: {"a": 1.0})
        with pytest.raises(RuntimeError, match="collection failed"):
            refresh_durations.main(["--ci-run", "42"])
    assert (tmp_path / ".test_durations").read_text() == saved


def test_collection_normalizes_group_suffix_without_removing_parameter_at_sign(tmp_path, monkeypatch):
    from tests import duration_coverage

    def collect(args, **kwargs):
        assert kwargs["env"]["PYTEST_ADDOPTS"] == ""
        assert kwargs["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == ""
        assert args[-3:] == ["0", "-o", "addopts="]
        return subprocess.CompletedProcess(args, 0, "tests/test_probe.py::test_one[x@y]@group\n", "")

    monkeypatch.setattr(duration_coverage.subprocess, "run", collect)
    assert duration_coverage.collected_tests(tmp_path) == ["tests/test_probe.py::test_one[x@y]"]


def test_empty_collection_is_refused(tmp_path, monkeypatch):
    from tests import duration_coverage

    monkeypatch.setattr(
        duration_coverage.subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "", "")
    )
    with pytest.raises(RuntimeError, match="No tests collected"):
        duration_coverage.collected_tests(tmp_path)


def test_collection_splits_the_suite_across_processes_without_assertion_rewriting(tmp_path, monkeypatch):
    from tests import duration_coverage

    (tmp_path / "tests/sub").mkdir(parents=True)
    files = ["tests/sub/test_c.py", "tests/test_a.py", "tests/test_b.py"]
    for path in files:
        (tmp_path / path).write_text("")
    calls = []

    def collect(args, **kwargs):
        options = args[args.index("--collect-only") :]
        assert args[3] == "tests/"
        assert options[:5] == ["--collect-only", "-q", "--assert=plain", "-p", "no:cacheprovider"]
        kept = [path for path in files if f"--ignore={path}" not in args]
        calls.append(kept)
        output = "".join(f"{path}::test_{i}\n" for path in kept for i in (1, 2))
        return subprocess.CompletedProcess(args, 0 if kept != ["tests/test_a.py"] else 5, output, "")

    monkeypatch.setattr(duration_coverage.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(duration_coverage.subprocess, "run", collect)
    assert duration_coverage.collected_tests(tmp_path) == [f"{path}::test_{i}" for path in files for i in (1, 2)]
    assert sorted(calls) == [["tests/sub/test_c.py", "tests/test_b.py"], ["tests/test_a.py"]]


def test_a_failed_collection_process_fails_the_whole_collection(tmp_path, monkeypatch):
    from tests import duration_coverage

    (tmp_path / "tests").mkdir()
    for path in ("tests/test_a.py", "tests/test_b.py"):
        (tmp_path / path).write_text("")

    def collect(args, **kwargs):
        failed = "--ignore=tests/test_a.py" in args
        return subprocess.CompletedProcess(args, int(failed), "" if failed else "tests/test_a.py::t\n", "broken b")

    monkeypatch.setattr(duration_coverage.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(duration_coverage.subprocess, "run", collect)
    with pytest.raises(RuntimeError, match="broken b"):
        duration_coverage.collected_tests(tmp_path)


def test_adoption_writes_the_collection_it_checked(tmp_path, monkeypatch):
    complete = _suite(tmp_path, 3)
    (tmp_path / ".test_durations").write_text(json.dumps(complete))
    monkeypatch.setattr(dev_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(dev_durations, "collected_tests", lambda root: list(complete))
    dev_durations.main(["3.12", str(tmp_path / "missing"), "--collected", str(tmp_path / "collected.json")])
    assert json.loads((tmp_path / "collected.json").read_text()) == list(complete)
