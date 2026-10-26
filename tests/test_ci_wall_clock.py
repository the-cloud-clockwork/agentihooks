import ast
import textwrap
from pathlib import Path

import pytest


def test_discovers_an_unmarked_elapsed_bound_in_a_new_directory(tmp_path, capsys):
    from scripts.ci_wall_clock import main

    target = tmp_path / "tests/new_area/test_new_budget.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "import time\n"
        "def test_new_budget():\n"
        "    started = time.perf_counter()\n"
        "    elapsed = (time.perf_counter() - started) / 20\n"
        "    assert elapsed < 0.005\n"
    )
    assert main(["--root", str(tmp_path)]) == 1
    assert (
        "tests/new_area/test_new_budget.py:5: test_new_budget requires pytest.mark.wall_clock"
        in capsys.readouterr().out
    )
    target.write_text(
        "import pytest\n"
        + target.read_text().replace("def test_new_budget", "@pytest.mark.wall_clock\ndef test_new_budget")
    )
    assert main(["--root", str(tmp_path)]) == 0


@pytest.mark.parametrize(
    "source",
    [
        "import time\ndef test_budget():\n    start = time.monotonic()\n    assert time.monotonic() - start < 1",
        "from time import perf_counter as clock\nasync def test_budget():\n    start = clock()\n    elapsed = clock() - start\n    assert 0 <= elapsed <= 1",
        "import time as timer\nclass TestBudget:\n    def test_budget(self):\n        start = timer.time_ns()\n        self.assertLess(timer.time_ns() - start, 1000)",
        "import time\ndef measured():\n    start = time.monotonic()\n    return 'ok', time.monotonic() - start\ndef test_budget():\n    done, took = measured()\n    assert took < 1",
        "import time\ndef test_budget():\n    clock = time.perf_counter\n    start = clock()\n    assert clock() - start < 1",
        "import time\ndef test_budget():\n    starts = []\n    starts.append(time.monotonic())\n    assert all(later - earlier < 1 for earlier, later in zip(starts, starts[1:]))",
    ],
)
def test_rejects_real_elapsed_bounds_across_supported_test_forms(tmp_path, source, capsys):
    from scripts.ci_wall_clock import main

    target = tmp_path / "tests/test_budget.py"
    target.parent.mkdir()
    target.write_text(source)
    assert main(["--root", str(tmp_path)]) == 1
    assert "requires pytest.mark.wall_clock" in capsys.readouterr().out


@pytest.mark.parametrize(
    "marker",
    [
        "@pytest.mark.wall_clock",
        "@pytest.mark.wall_clock()",
        "pytestmark = pytest.mark.wall_clock",
        "pytestmark = [pytest.mark.wall_clock]",
    ],
)
def test_accepts_decorator_and_module_markers(tmp_path, marker):
    from scripts.ci_wall_clock import main

    target = tmp_path / "tests/test_budget.py"
    target.parent.mkdir()
    target.write_text(
        f"import time\nimport pytest\n{marker}\ndef test_budget():\n    start = time.monotonic()\n    assert time.monotonic() - start < 1\n"
    )
    assert main(["--root", str(tmp_path)]) == 0


def test_accepts_an_inherited_class_marker(tmp_path):
    from scripts.ci_wall_clock import main

    target = tmp_path / "tests/test_budget.py"
    target.parent.mkdir()
    target.write_text(
        textwrap.dedent("""
        import time
        import pytest
        @pytest.mark.wall_clock
        class TestBudget:
            def test_budget(self):
                start = time.monotonic()
                assert time.monotonic() - start < 1
    """)
    )
    assert main(["--root", str(tmp_path)]) == 0


def test_keeps_functional_result_checks_separate_from_elapsed_return_values(tmp_path):
    from scripts.ci_wall_clock import main

    target = tmp_path / "tests/test_budget.py"
    target.parent.mkdir()
    target.write_text(
        textwrap.dedent("""
        import time
        def measured():
            start = time.monotonic()
            return 0, time.monotonic() - start
        def test_result():
            result, elapsed = measured()
            assert result < 1
        def test_stored_duration():
            elapsed = 42
            assert elapsed < 50
        def test_ready_deadline():
            deadline = time.monotonic() + 5
            assert time.monotonic() < deadline
    """)
    )
    assert main(["--root", str(tmp_path)]) == 0


@pytest.mark.parametrize("clock_import", ["import time\nclock = time.monotonic", "from time import monotonic as clock"])
def test_follows_imported_test_helpers_without_importing_the_test_code(tmp_path, capsys, clock_import):
    from scripts.ci_wall_clock import main

    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "measurement.py").write_text(
        clock_import + "\ndef measured():\n    start = clock()\n    return 'ok', clock() - start\n"
    )
    (directory / "test_budget.py").write_text(
        "from tests.measurement import measured\nraise RuntimeError('must not import')\ndef test_budget():\n    assert measured()[1] < 1\n"
    )
    assert main(["--root", str(tmp_path)]) == 1
    assert "test_budget requires pytest.mark.wall_clock" in capsys.readouterr().out


def test_follows_clock_arguments_into_elapsed_helpers(tmp_path, capsys):
    from scripts.ci_wall_clock import main

    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "test_budget.py").write_text(
        "import time\ndef elapsed(start):\n    return time.monotonic() - start\ndef test_budget():\n    start = time.monotonic()\n    assert elapsed(start) < 1\n"
    )
    assert main(["--root", str(tmp_path)]) == 1
    assert "test_budget requires pytest.mark.wall_clock" in capsys.readouterr().out


@pytest.mark.parametrize("source", [None, "def test_broken("])
def test_refuses_an_empty_or_unparseable_test_tree(tmp_path, source):
    from scripts.ci_wall_clock import main

    if source is not None:
        directory = tmp_path / "tests"
        directory.mkdir()
        (directory / "test_broken.py").write_text(source)
    assert main(["--root", str(tmp_path)]) == 1


def test_lint_checks_the_head_tree_with_the_protected_timing_grader():
    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/test.yml").read_text())
    lint = workflow["jobs"]["lint"]
    step = next(step for step in lint["steps"] if step.get("name") == "Require wall clock markers on elapsed bounds")
    assert step["if"] == "${{ !cancelled() }}"
    assert 'cd "$RUNNER_TEMP/grader"' in step["run"]
    assert 'python -m scripts.ci_wall_clock --root "$GITHUB_WORKSPACE"' in step["run"]
    assert "lint" in workflow["jobs"]["gate-required"]["needs"]


@pytest.mark.parametrize("assertion", ["assertLess", "assertLessEqual", "assertGreater", "assertGreaterEqual"])
def test_discovers_each_unittest_elapsed_comparison(tmp_path, assertion, capsys):
    from scripts.ci_wall_clock import main

    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "test_budget.py").write_text(
        "import time\nclass TestBudget:\n    def test_budget(self):\n        started = time.monotonic()\n"
        f"        self.{assertion}(time.monotonic() - started, 1)\n"
    )
    assert main(["--root", str(tmp_path)]) == 1
    assert "TestBudget.test_budget requires pytest.mark.wall_clock" in capsys.readouterr().out


@pytest.mark.parametrize(
    "source",
    [
        "import time\ndef measured(start):\n    return time.monotonic() - start, 0\ndef test_budget():\n    sample = measured(time.monotonic())\n    alias = sample\n    assert alias[0] < 1",
        "import time\ndef measured(start):\n    return time.monotonic() - start, 0\ndef test_budget():\n    sample = measured(start=time.monotonic())\n    assert sample[-2] < 1",
        "import time\nstarts = []\ndef test_budget():\n    starts.append(time.monotonic())\n    assert time.monotonic() - starts[0] < 1",
        "import time\ndef test_budget():\n    clock = time.monotonic\n    now = clock\n    started = now()\n    assert now() - started < 1",
        "import time\ndef test_budget():\n    clock = time.monotonic\n    clock = clock\n    started = clock()\n    assert clock() - started < 1",
    ],
)
def test_discovers_aliased_samples_and_clock_functions(tmp_path, source, capsys):
    from scripts.ci_wall_clock import main

    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "test_budget.py").write_text(source)
    assert main(["--root", str(tmp_path)]) == 1
    assert "test_budget requires pytest.mark.wall_clock" in capsys.readouterr().out


@pytest.mark.parametrize(
    "source",
    [
        "def test_result():\n    choose = lambda: 1\n    value = choose()\n    assert value < 2",
        "import time\ndef test_stamp():\n    value = 1\n    value = value\n    assert time.monotonic() - value > 0",
        "import os\nimport time\ndef test_stamp():\n    assert time.monotonic() - os.getpid() > 0",
        "import time\ndef action():\n    pass\ndef test_stamp():\n    assert time.monotonic() - (action() or 0) > 0",
        "import time\nclass TestBudget:\n    def test_timestamp(self):\n        self.assertLess(time.time(), 9999999999)",
    ],
)
def test_does_not_treat_functional_values_as_clock_measurements(tmp_path, source):
    from scripts.ci_wall_clock import main

    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "test_result.py").write_text(source)
    assert main(["--root", str(tmp_path)]) == 0


def test_follows_transitive_helpers_and_stops_an_import_cycle(tmp_path, capsys):
    from scripts.ci_wall_clock import main

    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "first.py").write_text("from tests.second import measured\n")
    (directory / "second.py").write_text(
        "from tests.first import measured\nimport time\ndef measured(start):\n    return time.monotonic() - start\n"
    )
    (directory / "test_budget.py").write_text(
        "import time\nfrom tests.first import measured\ndef test_budget():\n    assert measured(time.monotonic()) < 1\n"
    )
    assert main(["--root", str(tmp_path)]) == 1
    assert "test_budget requires pytest.mark.wall_clock" in capsys.readouterr().out


def test_reports_every_bound_and_continues_after_a_parse_error(tmp_path, capsys, monkeypatch):
    from scripts.ci_wall_clock import main

    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "test_a_broken.py").write_text("def test_broken(")
    (directory / "test_b_budget.py").write_text(
        "import time\ndef test_one():\n    start = time.monotonic()\n    assert time.monotonic() - start < 1\n"
        "def test_two():\n    start = time.monotonic()\n    assert time.monotonic() - start < 2\n"
    )
    monkeypatch.chdir(tmp_path)
    assert main([]) == 1
    output = capsys.readouterr().out
    assert "Wall clock markers: 2 files, 2 elapsed bounds, 3 problems" in output
    assert "test_a_broken.py: cannot parse:" in output
    assert "test_one requires pytest.mark.wall_clock" in output
    assert "test_two requires pytest.mark.wall_clock" in output


def test_reports_an_empty_tree_with_an_actionable_error(tmp_path, capsys):
    from scripts.ci_wall_clock import main

    assert main(["--root", str(tmp_path)]) == 1
    assert "::error::No Python test files found" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "def XXXX():\n    start = time.monotonic()\n    return time.monotonic() - start\ndef test_result():\n    assert (lambda: 0)() < 1",
            0,
        ),
        (
            "def elapsed(start):\n    return time.monotonic() - start\ndef forwarding(start):\n    return elapsed(start)\ndef test_budget():\n    assert forwarding(time.monotonic()) < 1",
            1,
        ),
        (
            "def measured():\n    start = time.monotonic()\n    return time.monotonic() - start\ndef test_budget():\n    measure = measured\n    assert measure() < 1",
            1,
        ),
        ("def test_stamp(unknown):\n    assert time.monotonic() - unknown < 1", 0),
        ("def test_stamp(unknown):\n    assert time.monotonic() - unknown[0] < 1", 0),
        ("def test_stamp():\n    assert time.monotonic() - unknown() < 1", 0),
        ("def test_stamp(clock):\n    clock = clock\n    assert time.monotonic() - clock() < 1", 0),
        ("def test_stamp():\n    clock = factory()\n    assert time.monotonic() - clock() < 1", 0),
        ("def test_stamp(value):\n    value = value\n    assert time.monotonic() - value < 1", 0),
        ("def test_stamp(values):\n    values = values\n    assert time.monotonic() - values[0] < 1", 0),
        ("def empty():\n    pass\ndef test_stamp():\n    assert time.monotonic() - empty()[0] < 1", 0),
        ("def test_budget():\n    pair = (0, time.monotonic())\n    assert time.monotonic() - pair[1] < 1", 1),
        (
            "def measured(start):\n    return (0, time.monotonic() - start)\ndef test_budget():\n    assert measured(time.monotonic())[1] < 1",
            1,
        ),
        (
            "def measured(start):\n    return [time.monotonic() - start for _ in range(1)][0]\ndef test_budget():\n    assert measured(time.monotonic()) < 1",
            1,
        ),
        (
            "def measured(start):\n    saved = start\n    return +(saved - time.monotonic())\ndef test_budget():\n    assert measured(time.monotonic()) < 1",
            1,
        ),
        (
            "def elapsed(start):\n    return abs(time.monotonic() - start)\ndef test_budget():\n    assert elapsed(time.monotonic()) < 1",
            1,
        ),
        ("def test_metadata():\n    assert 2 - 1 < 3\n    assert time.monotonic() + time.monotonic() < 99999999999", 0),
        (
            "def test_budget():\n    start = time.monotonic()\n    sample = (time.monotonic() - start, 0)\n    assert sample[2] < 1",
            1,
        ),
    ],
)
def test_preserves_clock_provenance_through_expressions_and_unknown_values(tmp_path, body, expected):
    from scripts.ci_wall_clock import main

    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "test_budget.py").write_text("import time\n" + body)
    assert main(["--root", str(tmp_path)]) == expected


def test_keeps_two_class_diagnostics_and_the_complete_marked_and_unmarked_count(tmp_path, capsys):
    from scripts.ci_wall_clock import main

    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "test_classes.py").write_text(
        textwrap.dedent("""
        import time
        class TestFirst:
            def test_budget(self):
                start = time.monotonic()
                assert time.monotonic() - start < 1
        class TestSecond:
            def test_budget(self):
                start = time.monotonic()
                assert time.monotonic() - start < 1
    """)
    )
    (directory / "test_marked.py").write_text(
        "import time\nimport pytest\n@pytest.mark.wall_clock\ndef test_budget():\n    start = time.monotonic()\n    assert time.monotonic() - start < 1"
    )
    assert main(["--root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "Wall clock markers: 2 files, 3 elapsed bounds, 2 problems" in output
    assert "TestFirst.test_budget requires pytest.mark.wall_clock" in output
    assert "TestSecond.test_budget requires pytest.mark.wall_clock" in output


def test_scans_past_a_missing_import_to_find_transitive_helper_bounds(tmp_path, capsys):
    from scripts.ci_wall_clock import main

    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "measurement.py").write_text("from tests.inner import measured\n")
    (directory / "inner.py").write_text(
        "import time\ndef measured():\n    start = time.monotonic()\n    return time.monotonic() - start\n"
    )
    (directory / "test_budget.py").write_text(
        "from tests.missing import absent\nfrom tests.measurement import measured\ndef test_budget():\n    assert measured() < 1\n"
    )
    assert main(["--root", str(tmp_path)]) == 1
    assert "test_budget requires pytest.mark.wall_clock" in capsys.readouterr().out


def test_syntax_errors_name_the_source_that_failed_to_parse(tmp_path, capsys):
    from scripts.ci_wall_clock import main

    directory = tmp_path / "tests"
    directory.mkdir()
    broken = directory / "test_broken.py"
    broken.write_text("def test_broken(")
    assert main(["--root", str(tmp_path)]) == 1
    assert "(test_broken.py, line 1)" in capsys.readouterr().out
    broken.unlink()
    helper = directory / "z_helper.py"
    helper.write_text("def measured(")
    (directory / "a_test.py").write_text("from tests.z_helper import measured\n")
    with pytest.raises(SyntaxError) as error:
        main(["--root", str(tmp_path)])
    assert error.value.filename == str(helper)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "def measured(start):\n    return (0, time.monotonic() - start)\ndef test_budget():\n    started = time.monotonic()\n    assert measured(started)[1] < 1",
            1,
        ),
        (
            "def measured():\n    start = time.monotonic()\n    return (time.monotonic() - start, 0)\ndef test_result():\n    result = measured()\n    assert result[1] < 1",
            0,
        ),
        (
            "from unittest import TestCase\nclass TestBudget(TestCase):\n    def test_budget(self):\n        self.case = self\n        started = time.monotonic()\n        self.case.assertLess(time.monotonic() - started, 1)",
            1,
        ),
    ],
)
def test_keeps_indexed_helper_origins_and_nested_unittest_receivers(tmp_path, body, expected):
    from scripts.ci_wall_clock import main

    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "test_budget.py").write_text("import time\n" + body)
    assert main(["--root", str(tmp_path)]) == expected


def test_keeps_clock_and_numeric_arguments_distinct_inside_one_helper(tmp_path):
    from scripts.ci_wall_clock import main

    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "test_stamp.py").write_text(
        "import time\ndef stamp(clock_origin, offset):\n    return time.monotonic() - offset\n"
        "def test_stamp():\n    assert stamp(time.monotonic(), 0) > 0\n"
    )
    assert main(["--root", str(tmp_path)]) == 0


def test_parses_a_helper_once_however_many_test_files_share_it(tmp_path, monkeypatch, capsys):
    from scripts.ci_wall_clock import main

    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "measurement.py").write_text(
        "import time\ndef measured():\n    start = time.monotonic()\n    return time.monotonic() - start\n"
    )
    for index in range(3):
        (directory / f"test_budget_{index}.py").write_text(
            "from tests.measurement import measured\ndef test_budget():\n    assert measured() < 1\n"
        )
    parsed = []
    original = ast.parse

    def counted(*args, **kwargs):
        parsed.append(kwargs["filename"])
        return original(*args, **kwargs)

    monkeypatch.setattr(ast, "parse", counted)
    assert main(["--root", str(tmp_path)]) == 1
    assert sorted(parsed) == sorted(
        [*(str(path) for path in directory.glob("*.py")), str(directory / "measurement.py")]
    )
    assert "Wall clock markers: 4 files, 3 elapsed bounds, 3 problems" in capsys.readouterr().out


def test_reports_every_nested_bound_in_source_order(tmp_path, capsys):
    from scripts.ci_wall_clock import main

    target = tmp_path / "tests/test_budget.py"
    target.parent.mkdir()
    target.write_text(
        "import time\n"
        "def test_budget():\n"
        "    start = time.monotonic()\n"
        "    assert time.monotonic() - start < 1\n"
        "    if start:\n"
        "        assert time.monotonic() - start < 2\n"
        "        assert time.monotonic() - start < 3\n"
        "        done = True\n"
    )
    assert main(["--root", str(tmp_path)]) == 1
    assert [line for line in capsys.readouterr().out.splitlines() if line.startswith("::error::")] == [
        f"::error::tests/test_budget.py:{line}: test_budget requires pytest.mark.wall_clock" for line in (4, 6, 7)
    ]
