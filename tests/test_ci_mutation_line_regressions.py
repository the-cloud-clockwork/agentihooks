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


def test_context_node_ids_with_pipes_and_boundary_hits_keep_exact_line_choices(tmp_path):
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
    )
    first, middle, last = (f"tests/a.py::test_{name}[a|b]" for name in ("first", "middle", "last"))
    coverage, root = _measured(tmp_path, source, {first + "|run": [1], middle + "|run": [2], last + "|run": [3]})
    assert covered_mutant_tests(coverage, root, {"scripts.writer.x_write": [first, middle, last]}) == {
        "scripts.writer.x_write__mutmut_1": [middle],
        "scripts.writer.x_write__mutmut_2": [last],
    }


def test_only_the_function_header_is_renamed_when_a_body_contains_the_mutant_name(tmp_path):
    source = (
        "def x_write__mutmut_orig(value):\n"
        "    name = 'x_write__mutmut_1'\n"
        "    return value + 1\n"
        "def x_write__mutmut_1(value):\n"
        "    name = 'x_write__mutmut_1'\n"
        "    return value - 1\n"
    )
    first, second = "tests/a.py::first", "tests/a.py::second"
    coverage, root = _measured(tmp_path, source, {first + "|run": [2], second + "|run": [3]})
    assert covered_mutant_tests(coverage, root, {"scripts.writer.x_write": [first, second]}) == {
        "scripts.writer.x_write__mutmut_1": [second]
    }


def test_an_external_coverage_file_does_not_hide_a_later_valid_file(tmp_path, monkeypatch):
    source = "def x_write__mutmut_orig():\n    return 1\ndef x_write__mutmut_1():\n    return 2\n"
    test = "tests/a.py::first"
    coverage, root = _measured(tmp_path, source, {test + "|run": [2]})
    monkeypatch.setattr(
        CoverageData, "measured_files", lambda self: [str(tmp_path / "external.py"), str(root / "scripts/writer.py")]
    )
    assert covered_mutant_tests(coverage, root, {"scripts.writer.x_write": [test]}) == {
        "scripts.writer.x_write__mutmut_1": [test]
    }


def test_cache_fallback_handles_markers_in_the_original_function_and_unowned_tests():
    from scripts.ci_mutation.stats_cache import fresh_by_path, per_test_stats, result_by_path

    function = "scripts.writer.x_write__mutmut_helper"
    mutant = function + "__mutmut_1"
    fast, slow, absent = "tests/a.py::fast", "tests/a.py::slow", "tests/b.py::absent"
    records = [
        {"status": 0, "tests": {function: [fast], mutant: []}, "durations": {fast: 1}, "cpu": 1},
        {"status": 0, "tests": {function: [slow]}, "durations": {slow: 2}, "cpu": 2},
    ]
    cached = fresh_by_path(["tests/a.py"], records)["tests/a.py"]
    restored, _ = per_test_stats("tests/a.py", cached, 1, {function, mutant})
    assert restored["tests"][mutant] == [slow]
    result = {"tests": {function: [fast, absent], mutant: []}, "durations": {fast: 1}}
    assert result_by_path(result, ["tests/a.py"], {fast: ["tests/a.py"]})["tests/a.py"]["tests"][mutant] == []


def test_runner_applies_an_explicit_mutant_selection_and_does_not_run_an_empty_one(tmp_path, monkeypatch):
    import json
    import sys

    from scripts.ci_mutation import selection

    monkeypatch.chdir(tmp_path)
    selected = tmp_path / "selection.json"
    selected.write_text(json.dumps({"scripts/a.py": {"lines": [2], "tests": ["tests/a.py"]}}))
    fast, slow = "tests/a.py::fast", "tests/a.py::slow"
    calls = []

    class PytestRunner:
        def execute_pytest(self, params, **kwargs):
            return 0

        def run_tests(self, *, mutant_name, tests):
            calls.append((mutant_name, list(tests)))
            return 0

    def cli(args):
        worker = runner.PytestRunner()
        worker.run_tests(mutant_name="scripts.a.x_f__mutmut_1", tests=[fast, slow])
        worker.run_tests(mutant_name="scripts.a.x_f__mutmut_2", tests=[fast, slow])

    runner = SimpleNamespace(
        collect_source_file_mutation_data=None,
        PytestRunner=PytestRunner,
        mutmut=SimpleNamespace(
            tests_by_mangled_function_name={"scripts.a.x_f__mutmut_1": {fast}, "scripts.a.x_f__mutmut_2": set()}
        ),
        cli=cli,
    )
    monkeypatch.setitem(sys.modules, "mutmut", SimpleNamespace(__main__=runner))
    monkeypatch.setenv("CI", "true")
    for name in [name for name in sys.modules if name == "scripts" or name.startswith("scripts.")]:
        monkeypatch.setitem(sys.modules, name, sys.modules[name])
    selection.run_selected(selected, (0, 1))
    assert calls == [("scripts.a.x_f__mutmut_1", [fast])]
