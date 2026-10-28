from types import SimpleNamespace

from coverage import CoverageData

from scripts.ci_mutation.line_tests import covered_mutant_tests, merged_test_selections


def _measured(tmp_path, source, contexts):
    root = tmp_path / "mutants"
    path = root / "scripts/writer.py"
    path.parent.mkdir(parents=True)
    path.write_text(source)
    filename = tmp_path / "coverage"
    data = CoverageData(basename=str(filename))
    for context, lines in contexts.items():
        data.set_context(context)
        data.add_lines({str(path): lines})
    data.write()
    return filename, root


def test_teardown_and_later_safe_mutants_keep_their_own_covering_tests(tmp_path):
    source = (
        "def x_write__mutmut_orig(value):\n"
        "    first = value + 1\n"
        "    return first * 2\n"
        "def x_write__mutmut_1(value):\n"
        "    first = value - 1\n"
        "    return first * 2\n"
        "def x_write__mutmut_2(value):\n"
        "    first = value + 1\n"
        "    return first * 3\n"
        "def x_other__mutmut_orig(value):\n"
        "    return value\n"
        "def x_other__mutmut_1(value):\n"
        "    return None\n"
    )
    first, second = "tests/a.py::first", "tests/a.py::second"
    coverage, root = _measured(tmp_path, source, {first + "|unexpected": [2], second + "|teardown": [3]})
    tests = {"scripts.writer.x_write": [first, second]}
    assert covered_mutant_tests(coverage, root, tests) == {
        "scripts.writer.x_write__mutmut_2": [second],
        "scripts.writer.x_other__mutmut_1": [],
    }


def test_nonstatement_changed_lines_without_context_keep_the_function_fallback(tmp_path):
    source = (
        "def x_write__mutmut_orig(value):\n"
        "    return (\n"
        "        value + 1\n"
        "    )\n"
        "def x_write__mutmut_1(value):\n"
        "    return (\n"
        "        value - 1\n"
        "    )\n"
    )
    test = "tests/a.py::first"
    coverage, root = _measured(tmp_path, source, {test + "|run": [1, 2]})
    assert covered_mutant_tests(coverage, root, {"scripts.writer.x_write": [test]}) == {}


def test_external_and_removed_measured_files_are_ignored(tmp_path):
    source = "def x_write__mutmut_orig():\n    return 1\ndef x_write__mutmut_1():\n    return 2\n"
    coverage, root = _measured(tmp_path, source, {"tests/a.py::first|run": [2]})
    assert covered_mutant_tests(coverage, root / "other", {}) == {}
    (root / "scripts/writer.py").unlink()
    assert covered_mutant_tests(coverage, root, {}) == {}


def test_disjoint_buckets_and_function_names_with_mutant_markers_preserve_fallbacks():
    function = "scripts.writer.x_write__mutmut_helper"
    mutant = function + "__mutmut_1"
    records = [
        {"tests": {mutant: ["tests/a.py::fast"]}},
        {"tests": {function: ["tests/b.py::slow"]}},
        {"tests": {"scripts.other.x_write": ["tests/c.py::other"]}},
    ]
    assert merged_test_selections(records)[mutant] == {"tests/a.py::fast", "tests/b.py::slow"}


def test_empty_mutant_selection_survives_slicing_into_each_owning_file():
    from scripts.ci_mutation.stats_cache import result_by_path

    function = "scripts.writer.x_write__mutmut_helper"
    mutant = function + "__mutmut_1"
    tests = ["tests/a.py::fast", "tests/b.py::slow"]
    result = {"status": 0, "tests": {function: tests, mutant: []}, "durations": dict.fromkeys(tests, 1)}
    parts = result_by_path(result, ["tests/a.py", "tests/b.py"], dict(zip(tests, [["tests/a.py"], ["tests/b.py"]])))
    assert parts["tests/a.py"]["tests"][mutant] == []
    assert parts["tests/b.py"]["tests"][mutant] == []


def test_all_observed_killers_run_before_the_remaining_tests(tmp_path):
    from scripts.ci_mutation.killers import KillMemory

    memory = KillMemory(tmp_path / "killers")
    for index, test in enumerate(["tests/a.py::first", "tests/b.py::second"]):
        memory.mutant = f"scripts.writer.x_write__mutmut_{index}"
        memory.pytest_runtest_logreport(SimpleNamespace(failed=True, nodeid=test))
    restored = KillMemory(tmp_path / "killers")
    tests = ["tests/c.py::slow", "tests/a.py::first", "tests/b.py::second"]
    assert restored.killer("scripts.writer.x_write__mutmut_3") == "tests/b.py::second"
    assert restored.order("scripts.writer.x_write__mutmut_3", tests) == [tests[2], tests[1], tests[0]]
    assert restored.order("scripts.writer.x_write__mutmut_3", tests[:1]) == tests[:1]


def test_statistics_collection_records_real_line_selections_from_its_mutants_root(tmp_path, monkeypatch):
    import json
    import subprocess

    from scripts.ci_mutation import selection, stats_dependencies

    source = "def x_write__mutmut_orig():\n    return 1\ndef x_write__mutmut_1():\n    return 2\n"
    test = "tests/a.py::first"
    coverage, root = _measured(tmp_path, source, {test + "|run": [2]})
    output = tmp_path / "stats.json"
    coverage.rename(output.with_suffix(".coverage"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MUTATION_STATS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("MUTATION_STATS_ROOT", str(tmp_path))
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: "scripts/writer.py\0")
    monkeypatch.setattr(stats_dependencies, "measured_dependencies", lambda *args: {})
    engine = SimpleNamespace(
        tests_by_mangled_function_name={"scripts.writer.x_write": {test}},
        duration_by_test={test: 1},
    )
    runner = SimpleNamespace(mutmut=engine)
    test_runner = SimpleNamespace(_pytest_add_cli_args=[], run_stats=lambda **kwargs: 0)
    selection.collect_shard_stats(runner, test_runner, [test], output, str(root / "temp"))
    assert json.loads(output.read_text())["tests"]["scripts.writer.x_write__mutmut_1"] == [test]
