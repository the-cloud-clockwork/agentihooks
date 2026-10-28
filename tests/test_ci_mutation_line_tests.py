from coverage import CoverageData


SOURCE = """\
def x_write__mutmut_orig(first):
    if first:
        return 1
    return 2

def x_write__mutmut_1(first):
    if first:
        return 3
    return 2

def x_write__mutmut_2(first):
    if first:
        return 1
    return 4
"""


def _coverage(tmp_path, contexts):
    root = tmp_path / "mutants"
    path = root / "scripts/writer.py"
    path.parent.mkdir(parents=True)
    path.write_text(SOURCE)
    filename = tmp_path / ".coverage"
    data = CoverageData(basename=str(filename))
    for context, lines in contexts.items():
        data.set_context(context)
        data.add_lines({str(path): lines})
    data.write()
    return filename, root


def test_each_mutant_keeps_every_test_covering_its_own_line_and_untraced_calls(tmp_path):
    from scripts.ci_mutation.line_tests import covered_mutant_tests

    filename, root = _coverage(
        tmp_path,
        {
            "tests/a.py::test_first|run": [1, 2, 3],
            "tests/a.py::test_second|run": [1, 2, 4],
            "tests/a.py::test_both|setup": [1, 2, 3, 4],
        },
    )
    tests = {
        "scripts.writer.x_write": {
            "tests/a.py::test_first",
            "tests/a.py::test_second",
            "tests/a.py::test_both",
            "tests/a.py::test_subprocess",
        }
    }
    assert covered_mutant_tests(filename, root, tests) == {
        "scripts.writer.x_write__mutmut_1": [
            "tests/a.py::test_both",
            "tests/a.py::test_first",
            "tests/a.py::test_subprocess",
        ],
        "scripts.writer.x_write__mutmut_2": [
            "tests/a.py::test_both",
            "tests/a.py::test_second",
            "tests/a.py::test_subprocess",
        ],
    }


def test_shared_or_absent_line_contexts_preserve_function_selection(tmp_path):
    from scripts.ci_mutation.line_tests import covered_mutant_tests

    filename, root = _coverage(tmp_path, {"": [1, 2, 3, 4]})
    tests = {"scripts.writer.x_write": {"tests/a.py::test_first"}}
    assert covered_mutant_tests(filename, root, tests) == {}
    assert covered_mutant_tests(tmp_path / "missing", root, tests) == {}


def test_statistics_cache_keeps_mutant_selection_and_planning_uses_its_cost():
    from scripts.ci_mutation.measured import measured_costs
    from scripts.ci_mutation.stats_cache import fresh_by_path, per_test_stats

    function = "scripts.writer.x_write"
    mutant = function + "__mutmut_1"
    tests = {function: ["tests/a.py::test_fast", "tests/a.py::test_slow"], mutant: ["tests/a.py::test_fast"]}
    record = {
        "status": 0,
        "tests": tests,
        "durations": {"tests/a.py::test_fast": 1, "tests/a.py::test_slow": 100},
        "cpu": 101,
    }
    sliced = fresh_by_path(["tests/a.py"], [record])
    restored, _ = per_test_stats("tests/a.py", sliced["tests/a.py"], 1, set(tests))
    assert restored["tests"] == tests
    assert measured_costs([mutant], [restored])[mutant] < 10
