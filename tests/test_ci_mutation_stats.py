import json
from pathlib import Path

import pytest

from scripts.ci_mutation.stats import SharedStats, load_parts, stats_key, write_part

SELECTED = {"hooks/a.py": ({3, 1}, ["tests/test_b.py", "tests/test_a.py"]), "hooks/b.py": (set(), [])}


def test_key_covers_the_head_and_every_selected_path_line_and_test():
    key = stats_key("abc", SELECTED)
    assert key == "6df05fe5947c94da6e3e1c5262188fc9769ba80a02104800600414894c163861"
    assert key == stats_key(
        "abc", {"hooks/b.py": (set(), []), "hooks/a.py": ({1, 3}, ["tests/test_a.py", "tests/test_b.py"])}
    )
    assert key != stats_key("abd", SELECTED)
    assert key != stats_key("abc", {**SELECTED, "hooks/a.py": ({1}, ["tests/test_a.py", "tests/test_b.py"])})
    assert key != stats_key("abc", {**SELECTED, "hooks/a.py": ({1, 3}, ["tests/test_a.py"])})
    assert key != stats_key("abc", {"hooks/a.py": SELECTED["hooks/a.py"]})


def test_group_folder_keeps_the_head_and_part():
    stats = SharedStats(Path("/stats"), "abc", (1, 2))
    assert stats.group(3) == SharedStats(Path("/stats/group-3"), "abc", (1, 2))
    assert SharedStats(Path("/stats"), "abc").part is None


def test_parts_round_trip_in_part_order(tmp_path):
    first, second = (
        [{"tests": {"f": ["t1"]}, "durations": {"t1": 1}, "cpu": 2}],
        [{"tests": {}, "durations": {}, "cpu": 0}],
    )
    write_part(tmp_path / "group-0/part-1.json", "k", (1, 2), second)
    write_part(tmp_path / "group-0/part-0.json", "k", (0, 2), first)
    assert json.loads((tmp_path / "group-0/part-1.json").read_text()) == {
        "key": "k",
        "part": 1,
        "parts": 2,
        "results": second,
    }
    assert load_parts(tmp_path / "group-0", "k") == (first + second, "")


def test_an_empty_part_still_counts(tmp_path):
    write_part(tmp_path / "part-0.json", "k", (0, 1), [])
    assert load_parts(tmp_path, "k") == ([], "")


def test_missing_folder_or_parts_name_what_is_missing(tmp_path):
    assert load_parts(tmp_path / "absent", "k") == (
        [],
        f"mutation stats missing: no stats part in {tmp_path / 'absent'}",
    )
    (tmp_path / "empty").mkdir()
    assert load_parts(tmp_path / "empty", "k")[1].startswith("mutation stats missing: no stats part")
    write_part(tmp_path / "part-0.json", "k", (0, 3), [])
    write_part(tmp_path / "part-2.json", "k", (2, 3), [])
    assert load_parts(tmp_path, "k") == ([], "mutation stats missing: found parts [0, 2] of [3]")


def test_parts_that_disagree_on_their_count_are_missing(tmp_path):
    write_part(tmp_path / "part-0.json", "k", (0, 2), [])
    write_part(tmp_path / "part-1.json", "k", (1, 3), [])
    assert load_parts(tmp_path, "k") == ([], "mutation stats missing: found parts [0, 1] of [2, 3]")


def test_a_part_collected_for_another_commit_or_selection_is_stale(tmp_path):
    write_part(tmp_path / "part-0.json", "k", (0, 3), [{"tests": {}, "durations": {}, "cpu": 0}])
    write_part(tmp_path / "part-1.json", "old", (1, 3), [])
    write_part(tmp_path / "part-2.json", "old", (2, 3), [])
    assert load_parts(tmp_path, "k") == (
        [],
        "mutation stats stale: parts [1, 2] were collected for another commit or selection",
    )


def test_measured_dependencies_include_collection_phases_and_preloaded_modules(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from coverage import CoverageData

    from scripts.ci_mutation.stats_dependencies import measured_dependencies

    root = tmp_path / "source"
    work = tmp_path / "mutants"
    paths = ["global.py", "setup.py", "run.py", "teardown.py", "loaded.py", "other.py"]
    for path in paths + ["untracked.py"]:
        for prefix in (root, work):
            (prefix / path).parent.mkdir(parents=True, exist_ok=True)
            (prefix / path).write_text("value = 1\n")
    coverage_file = tmp_path / "coverage"
    data = CoverageData(basename=str(coverage_file))
    data.set_context("")
    data.add_lines(
        {
            str(work / "global.py"): {1},
            str(work / "00_missing.py"): {1},
            str(work / "untracked.py"): {1},
            str(tmp_path / "00_external.py"): {1},
        }
    )
    first = "tests/test_a.py::test_a[one|two]"
    second = "tests/test_b.py::test_b"
    for phase in ("setup", "run", "teardown"):
        data.set_context(first + "|" + phase)
        data.add_lines({str(work / f"{phase}.py"): {1}})
    data.set_context(second + "|run")
    data.add_lines({str(work / "other.py"): {1}})
    data.write()
    monkeypatch.setitem(sys.modules, "stats_loaded_probe", SimpleNamespace(__file__=str(root / "loaded.py")))
    monkeypatch.setitem(sys.modules, "stats_untracked_probe", SimpleNamespace(__file__=str(root / "untracked.py")))
    tracked = set(paths) | {"00_missing.py"}
    found = measured_dependencies(coverage_file, work, root, [first, second], tracked)
    assert found == {
        first: ["global.py", "loaded.py", "run.py", "setup.py", "teardown.py"],
        second: ["global.py", "loaded.py", "other.py"],
    }
    assert measured_dependencies(coverage_file, work, root, ["tests/test_missing.py::test_missing"], tracked) == {}


def test_statistics_collect_runtime_dependencies_with_plugin_discovery_disabled(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    from types import SimpleNamespace

    from scripts.ci_mutation.selection import collect_shard_stats

    original = tmp_path / "original"
    work = tmp_path / "work"
    node = "tests/test_sample.py::test_value"
    files = {
        "scripts/sample.py": "def value():\n    return 1\n",
        "tests/test_sample.py": "from scripts.sample import value\nfrom scripts import untracked\ndef test_value():\n    assert value() == 1\n",
    }
    for root in (original, work / "mutants"):
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
    for root in (original, work / "mutants"):
        (root / "scripts/untracked.py").write_text("value = 2\n")
    subprocess.run(["git", "init", "-q", str(original)], check=True)
    subprocess.run(["git", "add", *files], cwd=original, check=True)
    monkeypatch.chdir(work)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("MUTATION_STATS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("MUTATION_STATS_ROOT", str(original))
    monkeypatch.setenv("COVERAGE_FILE", str(tmp_path / "before"))
    engine = SimpleNamespace(
        tests_by_mangled_function_name={"scripts.sample.x_value": {node}}, duration_by_test={node: 1}
    )

    class Runner:
        _pytest_add_cli_args = []

        def run_stats(self, *, tests):
            done = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", *self._pytest_add_cli_args, *tests],
                cwd=work / "mutants",
                env=dict(os.environ),
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert done.returncode == 0, done.stdout + done.stderr
            return done.returncode

    output = tmp_path / "part.json"
    collect_shard_stats(
        SimpleNamespace(mutmut=engine), Runner(), ["tests/test_sample.py"], output, str(tmp_path / "base")
    )
    result = json.loads(output.read_text())
    assert result["status"] == 0
    assert result["durations"] == {node: 1}
    assert "scripts/sample.py" in result["dependencies"][node]
    assert "tests/test_sample.py" in result["dependencies"][node]
    assert "scripts/untracked.py" not in result["dependencies"][node]


@pytest.mark.wall_clock
def test_a_stats_bucket_returns_while_a_test_left_a_thread_running(tmp_path, monkeypatch):
    import threading
    import time
    from types import SimpleNamespace

    from scripts.ci_mutation.selection import run_stats_buckets

    node = "tests/test_sample.py::test_value"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MUTATION_STATS_CACHE", raising=False)

    class Runner:
        _pytest_add_cli_args = []

        def run_stats(self, *, tests):
            threading.Thread(target=time.sleep, args=(60,)).start()
            engine.duration_by_test[node] = 1
            return 0

    engine = SimpleNamespace(tests_by_mangled_function_name={}, duration_by_test={})
    started = time.monotonic()
    results = run_stats_buckets(SimpleNamespace(mutmut=engine), Runner(), [["tests/test_sample.py"]], tmp_path)
    assert time.monotonic() - started < 30
    assert [result["status"] for result in results] == [0]
    assert results[0]["durations"] == {node: 1}
    assert results[0]["files"] == ["tests/test_sample.py"]


@pytest.mark.wall_clock
def test_a_failing_stats_bucket_reports_exit_one_and_its_error_while_a_thread_runs(tmp_path, monkeypatch, capfd):
    import threading
    import time
    from types import SimpleNamespace

    from scripts.ci_mutation.selection import run_stats_buckets

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MUTATION_STATS_CACHE", raising=False)

    class Runner:
        _pytest_add_cli_args = []

        def run_stats(self, *, tests):
            threading.Thread(target=time.sleep, args=(60,)).start()
            raise RuntimeError("stats broke")

    engine = SimpleNamespace(tests_by_mangled_function_name={}, duration_by_test={})
    started = time.monotonic()
    with pytest.raises(SystemExit) as stopped:
        run_stats_buckets(SimpleNamespace(mutmut=engine), Runner(), [["tests/test_sample.py"]], tmp_path)
    assert time.monotonic() - started < 30
    assert stopped.value.code == 1
    output = capfd.readouterr()
    assert "failed to collect stats. runner returned [1]" in output.out
    assert "RuntimeError: stats broke" in output.err


def test_a_stats_bucket_passes_its_arguments_and_exits_zero_or_one_in_process(monkeypatch, capfd):
    from scripts.ci_mutation import selection

    arguments = (object(), object(), ["tests/test_sample.py"], Path("stats-0.json"), "base")
    calls = []

    class Exited(Exception):
        pass

    def exit_now(code):
        raise Exited(code)

    def collect(*given):
        calls.append(given)
        if len(calls) == 2:
            raise RuntimeError("stats broke")

    monkeypatch.setattr(selection, "collect_shard_stats", collect)
    monkeypatch.setattr(selection.os, "_exit", exit_now)
    codes = []
    for _ in range(2):
        with pytest.raises(Exited) as exited:
            selection.collect_and_exit(*arguments)
        codes.append(exited.value.args[0])
    assert codes == [0, 1]
    assert calls == [arguments, arguments]
    assert "RuntimeError: stats broke" in capfd.readouterr().err
