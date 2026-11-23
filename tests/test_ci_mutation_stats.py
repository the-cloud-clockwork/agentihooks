import inspect
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

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
        "split": "",
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


def test_parts_that_split_the_tests_on_different_timings_are_refused(tmp_path):
    write_part(tmp_path / "part-0.json", "k", (0, 2), [{"tests": {}, "durations": {}, "cpu": 0}], "first")
    write_part(tmp_path / "part-1.json", "k", (1, 2), [], "second")
    assert load_parts(tmp_path, "k") == (
        [],
        "mutation stats split differs: the parts divided the tests on different timings",
    )
    write_part(tmp_path / "part-1.json", "k", (1, 2), [], "first")
    assert load_parts(tmp_path, "k") == ([{"tests": {}, "durations": {}, "cpu": 0}], "")


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


def test_measured_dependencies_keep_only_the_test_modules_a_node_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from types import SimpleNamespace

    from coverage import CoverageData

    from scripts.ci_mutation.stats_dependencies import measured_dependencies

    root = tmp_path / "source"
    work = tmp_path / "mutants"
    collected = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/helpers.py"]
    loaded = ["tests/test_imported.py", "scripts/test_names.py"]
    for path in collected + loaded:
        for prefix in (root, work):
            (prefix / path).parent.mkdir(parents=True, exist_ok=True)
            (prefix / path).write_text("value = 1\n")
    coverage_file = tmp_path / "coverage"
    data = CoverageData(basename=str(coverage_file))
    data.set_context("")
    data.add_lines({str(work / path): {1} for path in collected})
    first = "tests/test_a.py::test_a"
    second = "tests/test_b.py::test_b"
    data.set_context(first + "|run")
    data.add_lines({str(work / "tests/test_a.py"): {1}, str(work / "tests/test_c.py"): {1}})
    data.set_context(second + "|run")
    data.add_lines({str(work / "tests/test_b.py"): {1}})
    data.write()
    for path in loaded:
        monkeypatch.setitem(sys.modules, f"stats_probe_{Path(path).stem}", SimpleNamespace(__file__=str(root / path)))
    found = measured_dependencies(coverage_file, work, root, [first, second], set(collected + loaded))
    assert found == {
        first: ["scripts/test_names.py", "tests/helpers.py", "tests/test_a.py", "tests/test_c.py"],
        second: ["scripts/test_names.py", "tests/helpers.py", "tests/test_b.py"],
    }


def place_flake_ledger(tests: Path) -> None:
    (tests / "__init__.py").touch()
    shutil.copy(Path(__file__).with_name("flake_ledger.py"), tests)


def stats_recorder_only() -> SimpleNamespace:
    """Stand in for mutmut's Config.get: answer its stats recorder, which reads the config on every trampolined call
    while a mutation run collects stats, and fail every other reader."""
    frame = inspect.currentframe()
    caller = frame.f_back if frame else None
    if caller is None or caller.f_code.co_name != "record_trampoline_hit":
        raise AssertionError("mutmut config read")
    return SimpleNamespace(source_paths=[], max_stack_depth=-1)


@pytest.mark.parametrize("inherited", ["before", None])
def test_statistics_collect_runtime_dependencies_with_plugin_discovery_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inherited: str | None
) -> None:
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
    place_flake_ledger(work / "mutants" / "tests")
    subprocess.run(["git", "init", "-q", str(original)], check=True)
    subprocess.run(["git", "add", *files], cwd=original, check=True)
    monkeypatch.chdir(work)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    # collect_shard_stats sets MUTANT_UNDER_TEST; restoring it keeps a mutation run's later tests on their mutant.
    monkeypatch.setenv("MUTANT_UNDER_TEST", os.environ.get("MUTANT_UNDER_TEST", ""))
    monkeypatch.setenv("MUTATION_STATS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("MUTATION_STATS_ROOT", str(original))
    if inherited:
        monkeypatch.setenv("COVERAGE_FILE", str(tmp_path / inherited))
    else:
        monkeypatch.delenv("COVERAGE_FILE", raising=False)
    engine = SimpleNamespace(
        tests_by_mangled_function_name={"scripts.sample.x_value": {node}}, duration_by_test={node: 1}
    )

    exported = []

    class Runner:
        _pytest_add_cli_args = []

        def run_stats(self, *, tests):
            exported.append(os.environ.get("COVERAGE_FILE"))
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
    assert exported == [None]
    assert (
        tmp_path / "part.coveragerc"
    ).read_text() == f"[run]\ndata_file = {(tmp_path / 'part.coverage').resolve()}\n"
    assert result["status"] == 0
    assert result["durations"] == {node: 1}
    assert "scripts/sample.py" in result["dependencies"][node]
    assert "tests/test_sample.py" in result["dependencies"][node]
    assert "scripts/untracked.py" not in result["dependencies"][node]


def test_a_stats_bucket_of_only_wall_clock_tests_collects_nothing_and_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import subprocess
    import sys
    from types import SimpleNamespace

    from scripts.ci_mutation.selection import collect_shard_stats

    work = tmp_path / "work"
    test = work / "mutants" / "tests" / "test_sample.py"
    test.parent.mkdir(parents=True)
    test.write_text("import pytest\n@pytest.mark.wall_clock\ndef test_value():\n    assert True\n")
    place_flake_ledger(test.parent)
    monkeypatch.chdir(work)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("MUTANT_UNDER_TEST", os.environ.get("MUTANT_UNDER_TEST", ""))
    monkeypatch.delenv("MUTATION_STATS_CACHE", raising=False)
    engine = SimpleNamespace(tests_by_mangled_function_name={}, duration_by_test={})
    codes = []

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
            codes.append(done.returncode)
            return done.returncode

    output = tmp_path / "part.json"
    node = "tests/test_sample.py::test_value"
    collect_shard_stats(SimpleNamespace(mutmut=engine), Runner(), [node], output, str(tmp_path / "base"))
    result = json.loads(output.read_text())
    assert codes == [pytest.ExitCode.NO_TESTS_COLLECTED]
    assert result["status"] == 0
    assert result["durations"] == {}
    assert result["files"] == [node]


def test_a_stats_rerun_passes_a_class_based_property_test_on_its_fresh_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import subprocess
    import sys
    from types import SimpleNamespace

    from scripts.ci_mutation.selection import collect_shard_stats

    root = Path(__file__).resolve().parents[1]
    work = tmp_path / "work"
    work.mkdir()
    module = work / "test_rerun_property.py"
    module.write_text(
        "from hypothesis import given, strategies as st\n"
        "from tests.context.test_guard_properties import property_settings\n"
        "class TestRerun:\n"
        "    @property_settings\n"
        "    @given(value=st.integers())\n"
        "    def test_value(self, request, value):\n"
        "        assert request.node.execution_count > 1\n"
    )
    monkeypatch.chdir(work)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("MUTANT_UNDER_TEST", os.environ.get("MUTANT_UNDER_TEST", ""))
    monkeypatch.setenv("PY_IGNORE_IMPORTMISMATCH", os.environ.get("PY_IGNORE_IMPORTMISMATCH", ""))
    monkeypatch.delenv("MUTATION_STATS_CACHE", raising=False)
    engine = SimpleNamespace(tests_by_mangled_function_name={}, duration_by_test={})
    outputs: list[str] = []

    class Runner:
        _pytest_add_cli_args: list[str] = []

        def run_stats(self, *, tests: list[str]) -> int:
            done = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", *self._pytest_add_cli_args, *tests],
                cwd=root,
                env=dict(os.environ),
                capture_output=True,
                text=True,
                timeout=30,
            )
            outputs.append(done.stdout + done.stderr)
            return done.returncode

    output = tmp_path / "part.json"
    collect_shard_stats(SimpleNamespace(mutmut=engine), Runner(), [str(module)], output, str(tmp_path / "base"))
    assert json.loads(output.read_text())["status"] == 0, outputs
    assert "1 passed" in outputs[0] and "1 rerun" in outputs[0], outputs


def stats_bucket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> tuple[Path, list[str]]:
    import os
    import subprocess
    import sys
    from types import SimpleNamespace

    from scripts.ci_mutation.selection import collect_shard_stats

    root = Path(__file__).resolve().parents[1]
    work = tmp_path / "work"
    work.mkdir()
    module = work / "test_bucket.py"
    module.write_text(source)
    monkeypatch.chdir(work)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("MUTANT_UNDER_TEST", os.environ.get("MUTANT_UNDER_TEST", ""))
    monkeypatch.setenv("PY_IGNORE_IMPORTMISMATCH", os.environ.get("PY_IGNORE_IMPORTMISMATCH", ""))
    monkeypatch.delenv("MUTATION_STATS_CACHE", raising=False)
    engine = SimpleNamespace(tests_by_mangled_function_name={}, duration_by_test={})
    outputs: list[str] = []

    class Runner:
        _pytest_add_cli_args: list[str] = []

        def run_stats(self, *, tests: list[str]) -> int:
            done = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", *self._pytest_add_cli_args, *tests],
                cwd=root,
                env=dict(os.environ),
                capture_output=True,
                text=True,
                timeout=60,
            )
            outputs.append(done.stdout + done.stderr)
            return done.returncode

    output = tmp_path / "stats-0.json"
    collect_shard_stats(SimpleNamespace(mutmut=engine), Runner(), [str(module)], output, str(tmp_path / "base"))
    return output, outputs


@pytest.mark.feature("mutation-stats-flake-ledger")
def test_a_stats_bucket_records_a_test_that_fails_once_then_passes_its_rerun_in_the_flake_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = tmp_path / "attempts"
    output, outputs = stats_bucket(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\n"
        "def test_value():\n"
        f"    attempts = Path({str(attempts)!r})\n"
        "    first = not attempts.exists()\n"
        "    attempts.touch()\n"
        "    assert not first, 'first attempt fails'\n",
    )
    result = json.loads(output.read_text())
    assert result["status"] == 0, outputs
    assert set(result) == {"status", "cpu", "tests", "durations", "files", "modules"}
    rows = [json.loads(line) for line in output.with_suffix(".flakes.jsonl").read_text().splitlines()]
    assert [(row["test"].rpartition("/")[2], row["outcome"], row["attempts"]) for row in rows] == [
        ("test_bucket.py::test_value", "flaky", 2)
    ], outputs
    assert len(rows[0]["errors"]) == 1 and "first attempt fails" in rows[0]["errors"][0]
    assert "first attempt fails" in rows[0]["tracebacks"][0]
    assert "Retry failure reports: " in outputs[0], outputs


@pytest.mark.feature("mutation-stats-flake-ledger")
def test_a_stats_bucket_with_no_rerun_points_the_flake_ledger_beside_its_output_and_writes_no_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = tmp_path / "seen"
    stale = tmp_path / "stats-0.flakes.jsonl"
    stale.write_text('{"test": "an earlier run"}\n')
    output, outputs = stats_bucket(
        tmp_path,
        monkeypatch,
        "import os\n"
        "from pathlib import Path\n"
        "def test_value():\n"
        f"    Path({str(seen)!r}).write_text(os.environ['CI_FLAKES'])\n",
    )
    assert json.loads(output.read_text())["status"] == 0, outputs
    assert seen.read_text() == str(stale)
    assert not stale.exists()
    assert "Retry failure reports" not in outputs[0], outputs


@pytest.mark.feature("mutation-stats-flake-ledger")
@pytest.mark.parametrize("caller", ["caller-flakes.jsonl", "", None])
@pytest.mark.parametrize("fails", [False, True])
def test_a_stats_bucket_gives_the_caller_back_its_flake_ledger_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller: str | None, fails: bool
) -> None:
    import os
    from types import SimpleNamespace

    from scripts.ci_mutation.selection import collect_shard_stats

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MUTANT_UNDER_TEST", os.environ.get("MUTANT_UNDER_TEST", ""))
    monkeypatch.setenv("PY_IGNORE_IMPORTMISMATCH", os.environ.get("PY_IGNORE_IMPORTMISMATCH", ""))
    monkeypatch.delenv("MUTATION_STATS_CACHE", raising=False)
    if caller is None:
        monkeypatch.delenv("CI_FLAKES", raising=False)
    else:
        monkeypatch.setenv("CI_FLAKES", caller)
    engine = SimpleNamespace(tests_by_mangled_function_name={}, duration_by_test={})
    seen: list[str | None] = []

    class Runner:
        _pytest_add_cli_args: list[str] = []

        def run_stats(self, *, tests: list[str]) -> int:
            seen.append(os.environ.get("CI_FLAKES"))
            if fails:
                os.environ.pop("CI_FLAKES")
                raise RuntimeError("stats failed")
            return 0

    output = tmp_path / "stats-0.json"
    if fails:
        with pytest.raises(RuntimeError, match="stats failed"):
            collect_shard_stats(SimpleNamespace(mutmut=engine), Runner(), [], output, str(tmp_path / "base"))
    else:
        collect_shard_stats(SimpleNamespace(mutmut=engine), Runner(), [], output, str(tmp_path / "base"))
    assert seen == [str(tmp_path / "stats-0.flakes.jsonl")]
    assert os.environ.get("CI_FLAKES") == caller


@pytest.mark.feature("mutation-stats-flake-ledger")
@pytest.mark.parametrize("mutants", [False, True])
@pytest.mark.parametrize(
    "rows", [[], ['{"test": "a::t", "outcome": "flaky"}\n', '{"test": "b::t", "outcome": "failed"}\n']]
)
def test_a_collected_stats_part_keeps_its_bucket_flake_rows_beside_the_part_and_out_of_the_stats_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: list[str], mutants: bool
) -> None:
    from collections.abc import Callable
    from types import SimpleNamespace

    from scripts.ci_mutation.selection import load_or_collect_stats

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MUTATION_STATS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("MUTATION_STATS_ROOT", str(tmp_path))
    (tmp_path / "changed-lines.json").write_text(json.dumps({"scripts/a.py": {"lines": [1]}}))
    config = SimpleNamespace(source_paths=[Path("scripts")], pytest_add_cli_args_test_selection=["tests/test_a.py"])
    runner = SimpleNamespace(
        Config=SimpleNamespace(get=lambda: config),
        SourceFileMutationData=lambda **kwargs: SimpleNamespace(
            load=lambda: None, exit_code_by_key={"m": None} if mutants else {}
        ),
    )
    results = [{"status": 0, "tests": {}, "durations": {}, "cpu": 0}]
    options: list[list[str]] = []

    def cached(
        root: Path,
        folder: Path,
        scope: dict[str, list[int]],
        files: list[str],
        given: list[str],
        collect: Callable[[list[str]], list[dict[str, object]]],
    ) -> list[dict[str, object]]:
        options.append(list(given))
        for index, row in enumerate(rows):
            Path(f"stats-{index}.flakes.jsonl").write_text(row)
        return results

    monkeypatch.setattr("scripts.ci_mutation.stats_cache.collect_cached_stats", cached)
    folder = tmp_path / ".mutation-stats" / "group-0"
    with pytest.raises(SystemExit) as done:
        load_or_collect_stats(
            runner,
            SimpleNamespace(_pytest_add_cli_args=["-q"]),
            ["scripts/a.py"],
            "collect",
            (str(folder / "part-0.json"), "key", "0", "1"),
        )
    assert done.value.code == 0
    assert options == ([["-q"]] if mutants else [])
    assert (folder / "flakes-0.jsonl").read_text() == ("".join(rows) if mutants else "")
    assert load_parts(folder, "key") == (results if mutants else [], "")


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
