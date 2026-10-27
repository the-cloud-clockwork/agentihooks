import ast
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ci_mutation.runner import run_gate


def _no_pytest(self, params, **kwargs):
    return 0


@pytest.mark.parametrize("lines", [{1}, {2}, {2, 5}])
def test_gate_runs_only_changed_line_mutants_including_untested_code(tmp_path, monkeypatch, lines):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks/__init__.py").touch()
    (tmp_path / "scripts/__init__.py").touch()
    shutil.copytree(
        Path(__file__).parents[1] / "scripts/ci_mutation",
        tmp_path / "scripts/ci_mutation",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (tmp_path / "scripts/sample.py").write_text(
        "def covered(value):\n    return value + 1\n\ndef untested(value):\n    return value + 2\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sample.py").write_text(
        "from scripts.sample import covered\n\ndef test_value():\n    assert covered(1) == 2\n"
    )
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    output = tmp_path / "evidence"
    report = run_gate(tmp_path, {"scripts/sample.py": lines}, output, 30)
    assert report["not_mutated"] == []
    result = report["files"][0]
    assert report["failed"] is (5 in lines)
    assert result["counts"] == ({"killed": 2, "no tests": 2} if 5 in lines else {"killed": 2} if 2 in lines else {})
    assert len(result["failures"]) == (2 if 5 in lines else 0)
    assert all(row["status"] == "no tests" and row["lines"] == [5] for row in result["failures"])
    assert result["untouched_survivors"] == []


def test_gate_collects_stats_when_a_selected_test_changes_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks/__init__.py").touch()
    (tmp_path / "scripts/__init__.py").touch()
    shutil.copytree(
        Path(__file__).parents[1] / "scripts/ci_mutation",
        tmp_path / "scripts/ci_mutation",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (tmp_path / "scripts/sample.py").write_text("def covered(value):\n    return value + 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sample.py").write_text(
        "from scripts.sample import covered\n\n"
        "def test_value(tmp_path, monkeypatch):\n    monkeypatch.chdir(tmp_path)\n    assert covered(1) == 2\n"
    )
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    report = run_gate(tmp_path, {"scripts/sample.py": {2}}, tmp_path / "evidence", 30)
    assert report["not_mutated"] == []
    assert report["files"][0]["counts"] == {"killed": 2}
    assert report["failed"] is False


@pytest.mark.parametrize("child, fault", [(False, False), (True, False), (True, True)])
def test_gate_imports_store_from_scratch_directory(tmp_path, monkeypatch, child, fault):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    root = Path(__file__).parents[1]
    for name in ("hooks", "scripts"):
        shutil.copytree(root / name, tmp_path / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (tmp_path / "tests").mkdir()
    probe = (
        "from scripts.swarm.store import _whole\n"
        "assert _whole('7') == 7\n"
        "assert _whole('0') == 0\n"
        "assert _whole('') is None\n"
    )
    test = "import os, subprocess, sys\nfrom pathlib import Path\n\n"
    test += "def test_store(tmp_path, monkeypatch):\n"
    if child:
        test += "".join("    " + line + "\n" for line in probe.splitlines())
        test += (
            "    result = subprocess.run([sys.executable, '-c', " + repr(probe) + "],\n"
            "        cwd=tmp_path, env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1])},\n"
            "        capture_output=True, text=True)\n"
            "    assert result.returncode == 0, result.stderr\n"
        )
    else:
        test += "    monkeypatch.chdir(tmp_path)\n" + "".join("    " + line + "\n" for line in probe.splitlines())
    (tmp_path / "tests/test_store.py").write_text(test)
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    store = tmp_path / "scripts/swarm/store.py"
    store.write_text(subprocess.check_output(["git", "show", "HEAD:scripts/swarm/store.py"], cwd=root, text=True))

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("-c", f"init.defaultBranch={tmp_path.name}", "init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    git("add", "scripts/swarm/store.py")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    replacement = "return int(raw) + 1 if raw else None" if fault else "return None if not raw else int(raw)"
    store.write_text(store.read_text().replace("return int(raw) if raw else None", replacement))
    git("add", "scripts/swarm/store.py")
    git("commit", "-m", "changed store")
    output = tmp_path / "evidence"
    command = [sys.executable, "-m", "scripts.ci_mutation", "--base", base, "--output", str(output), "--budget", "30"]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    report = json.loads((output / "report.json").read_text())
    assert result.returncode == int(fault), result.stdout + result.stderr
    assert report["files"] == [] if fault else report["files"][0]["path"] == "scripts/swarm/store.py"
    logs = "\n".join(path.read_text() for path in (tmp_path / "evidence").glob("*/run.log"))
    assert "FileNotFoundError" not in logs
    if fault:
        assert report["failed"] is True
        assert "AssertionError" in logs
        assert "assert 8 == 7" in logs
    else:
        assert report["not_mutated"] == [], logs
        assert report["files"][0]["counts"]["killed"] > 0
        assert report["failed"] is False


def _assert_units_run_their_own_tests(monkeypatch, selection, runner, test_calls):
    unit = "scripts.s.x_f__mutmut_1"
    units = []

    def shard_tests(tests, name, index):
        units.append((tests, name, index))
        return [test for test in tests if test in {"h", "b"}]

    with monkeypatch.context() as planned:
        planned.setattr(selection, "shard_tests", shard_tests)
        assert runner.PytestRunner().run_tests(mutant_name=unit, tests=["a"]) == 0
        runner.PytestRunner().run_tests(mutant_name=unit, tests=("h", "a", "b"))
        assert runner.PytestRunner().run_tests(mutant_name="unsplit", tests=[]) == 9
    assert units == [(["a"], unit, 1), (["h", "a", "b"], unit, 1), ([], "unsplit", 1)]
    assert test_calls[-2:] == [(unit, ["h", "b"]), ("unsplit", [])]
    assert len(test_calls) == 6


@pytest.mark.parametrize(
    "header", ["", '"""sample contract"""\n', '"""sample contract"""\nfrom __future__ import annotations\n']
)
def test_selection_passes_exact_lines_before_generation_and_reloads_source_packages(tmp_path, monkeypatch, header):
    from mutmut import configuration as engine_config

    from scripts.ci_mutation import selection as selection_module
    from scripts.ci_mutation.selection import run_selected

    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(engine_config, "_config", None)
    project = tmp_path / "mutants"
    (project / "scripts").mkdir(parents=True)
    (project / "hooks").mkdir()
    (project / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["hooks/"]\n')
    monkeypatch.setattr("os.cpu_count", lambda: 6)
    monkeypatch.setattr("os.sched_getaffinity", lambda pid: set(range(6)) if pid == 0 else set())
    monkeypatch.setattr("scripts.ci_mutation.selection.quota_cpus", lambda: None)
    selection = tmp_path / "lines.json"
    selection.write_text(
        json.dumps(
            {
                "scripts/sample.py": {"lines": [2, 5], "tests": ["tests/test_sample.py"]},
                "hooks/other.py": {"lines": [], "tests": ["tests/test_other.py"]},
            }
        )
    )
    test_runner = object()
    data = SimpleNamespace(exit_code_by_key={"selected": None})
    loaded = []

    def load():
        loaded.append(True)

    def mutation_data(*, path):
        assert path in {Path("scripts/sample.py"), Path("hooks/other.py")}
        return data

    config = SimpleNamespace(source_paths=[Path("hooks/")], pytest_add_cli_args_test_selection=["tests/test_sample.py"])
    collected = []

    def shard(root, files, count):
        assert (root, files, count) == (Path.cwd(), ["tests/test_sample.py"], 6)
        return [files]

    def collect_parallel(engine_runner, value, shards, work):
        assert engine_runner is runner
        assert value is test_runner
        assert loaded
        assert config.source_paths == [Path.cwd() / "mutants/hooks"]
        assert (shards, work) == ([["tests/test_sample.py"]], Path.cwd())
        collected.append(True)

    monkeypatch.setattr("scripts.ci_mutation.selection.stats_shards", shard)
    monkeypatch.setattr("scripts.ci_mutation.selection.collect_parallel_stats", collect_parallel)

    data.load = load
    engine = SimpleNamespace(
        tests_by_mangled_function_name={
            "scripts.sample.x_f": {"tests/test_sample.py::test_slow", "tests/test_other.py::test_o"},
            "scripts.sample.x_g": {"tests/test_sample.py::test_fast", "tests/test_sample.py::TestCase::test_m"},
            "hooks.other.x_h": {"tests/test_sample.py::test_slow", "tests/test_other.py::test_o"},
            "scripts.unselected.x_u": {"tests/test_sample.py::test_slow"},
        },
        duration_by_test={"tests/test_sample.py::test_slow": 2, "tests/test_sample.py::test_fast": 1},
    )
    engine.duration_by_test["tests/test_other.py::test_o"] = 3
    engine.duration_by_test["tests/test_sample.py::TestCase::test_m"] = 0
    test_calls = []

    class PytestRunner:
        execute_pytest = _no_pytest

        def run_tests(self, *, mutant_name, tests):
            assert isinstance(self, PytestRunner)
            test_calls.append((mutant_name, tests))
            return 9

    mutants = [(data, f"scripts.sample.x_f__mutmut_{n}", None) for n in range(1, 8)]
    runner = SimpleNamespace(
        collect_or_load_stats=None,
        collect_source_file_mutation_data=lambda *, mutant_names: (mutants, mutant_names),
        SourceFileMutationData=mutation_data,
        Config=SimpleNamespace(get=lambda: config),
        PytestRunner=PytestRunner,
        mutmut=engine,
    )
    mutmut = SimpleNamespace(__main__=runner)

    def control(value, tests):
        previous = os.environ.get("MUTANT_UNDER_TEST")
        os.environ["MUTANT_UNDER_TEST"] = value
        try:
            return runner.PytestRunner().run_tests(mutant_name=None, tests=tests)
        finally:
            if previous is None:
                os.environ.pop("MUTANT_UNDER_TEST")
            else:
                os.environ["MUTANT_UNDER_TEST"] = previous

    def cli(args):
        assert args == ["run", "--max-children", "6"]
        stream = __import__("io").StringIO()
        with monkeypatch.context() as generation:
            generation.chdir(tmp_path)
            names = runner.write_all_mutants_to_file(out=stream, source="source", filename=Path("scripts/sample.py"))
        assert names == ["selected"]
        assert stream.getvalue().endswith("generated = True\n")
        observations = []

        def observe():
            assert engine_config.Config.get().source_paths == [project / "hooks"]
            observations.append(Path.cwd())

        namespace = {"__file__": str(project / "scripts/sample.py"), "observe": observe}
        cwd = Path.cwd()
        engine_config.Config.reset()
        exec(stream.getvalue(), namespace)
        assert namespace["generated"] is True
        assert observations == [cwd]
        assert namespace.get("__doc__") == ("sample contract" if header else None)
        assert Path.cwd() == cwd
        assert engine_config.Config.get().source_paths == [project / "hooks"]
        (tmp_path / "copy").mkdir()
        copy = {"__file__": str(tmp_path / "copy/scripts/sample.py"), "observe": observe}
        engine_config.Config.reset()
        exec(stream.getvalue(), copy)
        assert observations == [cwd, cwd]
        assert engine_config.Config.get().source_paths == [project / "hooks"]
        (tmp_path / "inner").mkdir()
        (tmp_path / "inner/pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["scripts/"]\n')
        inner = {"__file__": str(tmp_path / "inner/scripts/sample.py"), "observe": lambda: None}
        engine_config.Config.reset()
        exec(stream.getvalue(), inner)
        assert engine_config.Config.get().source_paths == [Path("scripts/")]
        assert Path.cwd() == cwd
        assert calls == [("scripts/sample.py", "source", {2, 5})]
        assert runner.PytestRunner().run_tests(mutant_name=None, tests=[]) == 9
        assert runner.collect_or_load_stats(test_runner) is None
        assert collected == [True]
        assert runner.collect_source_file_mutation_data(mutant_names=("m",)) == ([mutants[1], mutants[4]], ("m",))
        assert config.source_paths == [Path("hooks/")]
        assert engine.tests_by_mangled_function_name == {
            "scripts.sample.x_f": {"tests/test_sample.py::test_slow"},
            "scripts.sample.x_g": {"tests/test_sample.py::test_fast", "tests/test_sample.py::TestCase::test_m"},
            "hooks.other.x_h": {"tests/test_other.py::test_o"},
            "scripts.unselected.x_u": set(),
        }
        assert control("", []) == 0
        assert control("fail", []) == 9
        runner.PytestRunner().run_tests(mutant_name=None, tests=["tests/test_x.py::t"])
        runner.PytestRunner().run_tests(mutant_name="m", tests=["tests/test_y.py::t"])
        assert test_calls == [
            (None, []),
            (
                None,
                [
                    "tests/test_sample.py::TestCase::test_m",
                    "tests/test_sample.py::test_fast",
                    "tests/test_sample.py::test_slow",
                    "tests/test_other.py::test_o",
                ],
            ),
            (None, ["tests/test_x.py::t"]),
            ("m", ["tests/test_y.py::t"]),
        ]
        _assert_units_run_their_own_tests(monkeypatch, selection_module, runner, test_calls)
        data.exit_code_by_key = {}
        with pytest.raises(SystemExit) as empty:
            runner.collect_or_load_stats(test_runner)
        assert empty.value.code == 0
        assert "scripts" not in sys.modules
        assert "scripts.ci_mutation" not in sys.modules
        assert "scripts.ci_mutation.report" not in sys.modules
        raise SystemExit(7)

    calls = []

    def selected(filename, source, lines):
        calls.append((filename, source, lines))
        return header + "observe()\ngenerated = True\n", ["selected"]

    monkeypatch.setattr("scripts.ci_mutation.selection.selected_mutants", selected)
    runner.cli = cli
    monkeypatch.setitem(sys.modules, "mutmut", mutmut)
    monkeypatch.setitem(sys.modules, "mutmut.__main__", SimpleNamespace(cli=cli))
    for name in [name for name in sys.modules if name == "scripts" or name.startswith("scripts.")]:
        monkeypatch.setitem(sys.modules, name, sys.modules[name])
    with pytest.raises(SystemExit) as error:
        run_selected(selection, (1, 3))
    assert error.value.code == 7


@pytest.mark.parametrize("mode", ["collect", "reuse", "empty"])
def test_selection_collects_one_stats_part_or_reuses_the_shared_stats(tmp_path, monkeypatch, mode):
    from collections import defaultdict

    from scripts.ci_mutation.selection import run_selected

    monkeypatch.setenv("CI", "true")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("os.sched_getaffinity", lambda pid: {0, 1})
    selection = tmp_path / "lines.json"
    selection.write_text(json.dumps({"scripts/sample.py": {"lines": [2], "tests": ["tests/test_sample.py"]}}))
    data = SimpleNamespace(exit_code_by_key={} if mode == "empty" else {"m": None}, load=lambda: None)
    config = SimpleNamespace(
        source_paths=[Path("scripts/")],
        pytest_add_cli_args_test_selection=["tests/test_c.py", "tests/test_a.py", "tests/test_b.py"],
    )
    engine = SimpleNamespace(tests_by_mangled_function_name=defaultdict(set), duration_by_test={}, stats_time=None)
    result = {
        "status": 0,
        "tests": {"scripts.sample.x_f": ["tests/test_sample.py::t"], "scripts.other.x_g": ["tests/test_o.py::t"]},
        "durations": {"tests/test_sample.py::t": 1},
        "cpu": 2,
    }
    shared = tmp_path / "shared.json"
    shared.write_text(json.dumps([result, {"status": 0, "tests": {}, "durations": {}, "cpu": 3}]))
    part = tmp_path / "stats/part-1.json"
    seen = []

    test_runner = object()

    def buckets(engine_runner, value, shards, work):
        assert engine_runner is runner
        assert value is test_runner
        assert config.source_paths == [tmp_path / "mutants/scripts"]
        seen.append((shards, work))
        return [result]

    def bucket_split(root, files, count):
        assert (root, files, count) == (tmp_path, ["tests/test_b.py"], 2)
        return [files]

    class PytestRunner:
        execute_pytest = _no_pytest

        def run_tests(self, *, mutant_name, tests):
            return 0

    saved = []
    runner = SimpleNamespace(
        SourceFileMutationData=lambda *, path: data,
        Config=SimpleNamespace(get=lambda: config),
        PytestRunner=PytestRunner,
        collect_source_file_mutation_data=lambda *, mutant_names: ([], {}),
        mutmut=engine,
        save_stats=lambda: saved.append(engine.stats_time),
    )

    def cli(args):
        if mode == "reuse":
            assert runner.collect_or_load_stats(test_runner) is None
            assert seen == []
            assert saved == [5]
            assert engine.tests_by_mangled_function_name == {
                "scripts.sample.x_f": {"tests/test_sample.py::t"},
                "scripts.other.x_g": set(),
            }
            assert engine.duration_by_test == {"tests/test_sample.py::t": 1}
            raise SystemExit(7)
        with pytest.raises(SystemExit) as done:
            runner.collect_or_load_stats(test_runner)
        assert done.value.code == 0
        assert config.source_paths == [Path("scripts/")]
        assert seen == ([] if mode == "empty" else [([["tests/test_b.py"]], tmp_path)])
        assert json.loads(part.read_text()) == {
            "key": "key",
            "part": 1,
            "parts": 3,
            "results": [] if mode == "empty" else [result],
        }
        assert saved == []
        raise SystemExit(7)

    runner.cli = cli
    monkeypatch.setattr("scripts.ci_mutation.selection.collect_test_ids", lambda value, files: files)
    monkeypatch.setattr("scripts.ci_mutation.selection.run_stats_buckets", buckets)
    monkeypatch.setattr("scripts.ci_mutation.selection.stats_shards", bucket_split)
    monkeypatch.setitem(sys.modules, "mutmut", SimpleNamespace(__main__=runner))
    monkeypatch.setitem(sys.modules, "mutmut.__main__", runner)
    for name in [name for name in sys.modules if name == "scripts" or name.startswith("scripts.")]:
        monkeypatch.setitem(sys.modules, name, sys.modules[name])
    stats = ["reuse", str(shared)] if mode == "reuse" else ["collect", str(part), "key", "1", "3"]
    with pytest.raises(SystemExit) as error:
        run_selected(selection, (0, 1), *stats)
    assert error.value.code == 7


def test_selection_refuses_an_unknown_stats_mode_before_touching_mutmut():
    from scripts.ci_mutation.selection import load_or_collect_stats

    with pytest.raises(ValueError, match="^unknown mutation stats mode 'other'$"):
        load_or_collect_stats(None, None, ["scripts/sample.py"], "other", ())


def test_multiline_operator_on_changed_line_is_mutated_and_unchanged_tokens_are_excluded(tmp_path, monkeypatch):
    from mutmut.configuration import Config
    from mutmut.mutation.pragma_handling import PragmaParseError

    from scripts.ci_mutation.selection import selected_mutants

    monkeypatch.setattr(
        Config, "get", lambda: SimpleNamespace(do_not_mutate_patterns=[], source_paths=[], max_stack_depth=-1)
    )
    source = "def f(a, b):\n    return (\n        a\n        - b\n    )\n"
    generated, names = selected_mutants("scripts/sample.py", source, {4})
    assert len(names) == 1
    assert "+ b" in generated
    generated, names = selected_mutants("scripts/sample.py", source, {1})
    assert names == []
    assert "+ b" not in generated
    source = "def f():\n    return 1\n\ndef g():\n    return 2\n"
    generated, names = selected_mutants("scripts/sample.py", source, {5})
    assert len(names) == 1
    assert all(name.startswith("x_g__") for name in names)
    with pytest.raises(PragmaParseError, match="scripts/sample.py"):
        selected_mutants("scripts/sample.py", "# pragma: no mutate end\n", {1})


def test_dataclass_methods_are_mutated_and_other_decorated_function_bodies_count_without_mutants(monkeypatch):
    from mutmut.configuration import Config

    from scripts.ci_mutation.selection import mutation_counts, selected_mutants

    monkeypatch.setattr(
        Config, "get", lambda: SimpleNamespace(do_not_mutate_patterns=[], source_paths=[], max_stack_depth=-1)
    )
    source = (
        "import dataclasses\n"
        "import functools\n"
        "\n"
        "@dataclasses.dataclass(frozen=True)\n"
        "class Launcher:\n"
        "    count: int = 1\n"
        "\n"
        "    def launch(self):\n"
        "        return self.count + 1\n"
        "\n"
        "@functools.lru_cache(maxsize=2)\n"
        "def cached(value):\n"
        "    return value - 1\n"
        "\n"
        "@functools.total_ordering\n"
        "class Ordered:\n"
        "    def __lt__(self, other):\n"
        "        return self.count < other.count\n"
    )
    generated, names = selected_mutants("scripts/sample.py", source, {9})
    assert len(names) == 2
    assert all(name.startswith("xǁLauncherǁlaunch__mutmut_") for name in names)
    assert "self.count - 1" in generated
    compile(generated, "scripts/sample.py", "exec")
    assert mutation_counts("scripts/sample.py", source, {9}) == (2, 2)
    assert selected_mutants("scripts/sample.py", source, {13, 18})[1] == []
    assert mutation_counts("scripts/sample.py", source, {13}) == (0, 2)
    assert (
        mutation_counts("scripts/sample.py", source, {18})[0]
        == 0
        < mutation_counts("scripts/sample.py", source, {18})[1]
    )
    assert mutation_counts("scripts/sample.py", source, {4, 11}) == (0, 0)


@pytest.mark.parametrize("total", [1, 2, 3, 7])
def test_shards_split_every_mutant_name_exactly_once_and_evenly(total):
    from scripts.ci_mutation.mutant_shards import shard_names

    names = [f"scripts.sample.x_f{n % 3}__mutmut_{n}" for n in range(1, 21)]
    shares = [shard_names(reversed(names), (index, total)) for index in range(total)]
    assert set().union(*shares) == set(names)
    assert sum(map(len, shares)) == len(names)
    assert max(map(len, shares)) - min(map(len, shares)) <= 1
    assert shares == [shard_names(names, (index, total)) for index in range(total)]


def test_selection_never_imports_the_mutants_tree_another_worker_is_writing(tmp_path, monkeypatch):
    from mutmut.configuration import Config

    from scripts.ci_mutation.selection import selected_mutants

    monkeypatch.setattr(
        Config, "get", lambda: SimpleNamespace(do_not_mutate_patterns=[], source_paths=[], max_stack_depth=-1)
    )
    package = tmp_path / "mutants/scripts/ci_mutation"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").touch()
    (package / "__init__.py").touch()
    (package / "report.py").touch()
    monkeypatch.syspath_prepend(str(tmp_path / "mutants"))
    for name in ("scripts", "scripts.ci_mutation", "scripts.ci_mutation.report"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    _, names = selected_mutants("scripts/sample.py", "def f(a, b):\n    return a - b\n", {2})
    assert len(names) == 1


def test_stats_shards_balance_by_duration_and_keep_xdist_groups_together(tmp_path):
    from scripts.ci_mutation.selection import IMPORT_GROUPS, stats_shards

    (tmp_path / "tests").mkdir()
    files = [f"tests/test_{name}.py" for name in "abcdef"]
    for path in files:
        (tmp_path / path).write_text("def test_x():\n    pass\n")
    (tmp_path / "tests/test_b.py").write_text('import pytest\n\npytestmark = pytest.mark.xdist_group("redis")\n')
    (tmp_path / "tests/test_d.py").write_text(
        "import pytest\n\n@pytest.mark.xdist_group(name='redis')\ndef test_x(): pass\n"
    )
    (tmp_path / "tests/test_f.py").write_text('import pytest\n\npytestmark = pytest.mark.xdist_group("mcp")\n')
    durations = {"tests/test_a.py::TestK::test_x": 5, "tests/test_c.py::t1": 2, "tests/test_c.py::t2": 2}
    durations |= {"tests/test_b.py::t": 1, "tests/test_d.py::t": 1, "tests/test_f.py::t": 1.5, "tests/other.py::t": 9}
    (tmp_path / ".test_durations").write_text(json.dumps(durations))
    assert stats_shards(tmp_path, files, 3) == [
        ["tests/test_a.py"],
        ["tests/test_c.py"],
        ["tests/test_b.py", "tests/test_d.py", "tests/test_e.py", "tests/test_f.py"],
    ]
    assert stats_shards(tmp_path, files, 1) == [files]
    assert stats_shards(tmp_path, files[:1], 8) == [["tests/test_a.py"]]
    bridge = tmp_path / "bridge"
    (bridge / "tests").mkdir(parents=True)
    marks = {"w": "", "x": '"one"', "y": '"one")\n@pytest.mark.xdist_group("two"', "z": '"two"'}
    for name, mark in marks.items():
        text = f"import pytest\n\n@pytest.mark.xdist_group({mark})\ndef test_x(): pass\n" if mark else "pass\n"
        (bridge / f"tests/test_{name}.py").write_text(text)
    bridged = [f"tests/test_{name}.py" for name in marks]
    assert stats_shards(bridge, bridged, 4) == [bridged[1:], bridged[:1]]
    (group,) = IMPORT_GROUPS
    imports = tmp_path / "imports"
    (imports / "tests").mkdir(parents=True)
    for name in "pqr":
        (imports / f"tests/test_{name}.py").write_text(
            f'import pytest\n\npytestmark = pytest.mark.xdist_group("{group}")\n'
        )
    imported = [f"tests/test_{name}.py" for name in "pqr"]
    assert stats_shards(imports, imported, 3) == [[path] for path in imported]
    (tmp_path / ".test_durations").unlink()
    assert stats_shards(tmp_path, files, 3) == [
        ["tests/test_b.py", "tests/test_d.py"],
        ["tests/test_a.py", "tests/test_e.py"],
        ["tests/test_c.py", "tests/test_f.py"],
    ]


def test_selected_test_ids_take_their_own_durations_share_their_file_shard_and_bound_kept_tests(tmp_path):
    from scripts.ci_mutation.selection import file_seconds, keep_selected_tests, stats_shards

    (tmp_path / "tests").mkdir()
    for name in "abc":
        (tmp_path / f"tests/test_{name}.py").write_text("def test_x():\n    pass\n")
    (tmp_path / "tests/test_b.py").write_text('import pytest\n\npytestmark = pytest.mark.xdist_group("port")\n')
    (tmp_path / "tests/test_d.py").write_text('import pytest\n\npytestmark = pytest.mark.xdist_group("port")\n')
    durations = {"tests/test_c.py::t1": 2, "tests/test_c.py::t1[p]@port": 1, "tests/test_c.py::t1x": 4}
    durations |= {"tests/test_c.py::t2": 8, "tests/test_a.py::TestK::test_x": 5, "tests/test_b.py::t": 6}
    (tmp_path / ".test_durations").write_text(json.dumps(durations))
    entries = [
        "tests/test_a.py",
        "tests/test_b.py::t",
        "tests/test_c.py::t1",
        "tests/test_c.py::t1x",
        "tests/test_d.py",
    ]
    seconds = file_seconds(tmp_path, entries)
    assert seconds == pytest.approx(
        {
            "tests/test_a.py": 5.01,
            "tests/test_b.py::t": 6.01,
            "tests/test_c.py::t1": 3.01,
            "tests/test_c.py::t1x": 4.01,
            "tests/test_d.py": 0.01,
        }
    )
    assert stats_shards(tmp_path, entries, 3) == [
        ["tests/test_c.py::t1", "tests/test_c.py::t1x"],
        ["tests/test_b.py::t", "tests/test_d.py"],
        ["tests/test_a.py"],
    ]
    reached = {
        "tests/test_c.py::t1[p]",
        "tests/test_c.py::t1x",
        "tests/test_c.py::t2",
        "tests/test_a.py::TestK::test_x",
        "tests/test_e.py::t",
    }
    stats = {"scripts.sample.x_f": set(reached), "scripts.other.x_g": set(reached)}
    allowed = {"scripts.sample.": {"tests/test_c.py::t1", "tests/test_a.py"}}
    assert keep_selected_tests(stats, allowed) == {"tests/test_c.py::t1[p]", "tests/test_a.py::TestK::test_x"}
    assert stats == {
        "scripts.sample.x_f": {"tests/test_c.py::t1[p]", "tests/test_a.py::TestK::test_x"},
        "scripts.other.x_g": set(),
    }


@pytest.mark.parametrize("total", [1, 2, 3, 6])
def test_stats_parts_split_files_by_duration_across_runners_even_inside_an_xdist_group(tmp_path, total):
    from scripts.ci_mutation.selection import part_files

    (tmp_path / "tests").mkdir()
    files = [f"tests/test_{name}.py" for name in "abcdef"]
    for path in files:
        (tmp_path / path).write_text("import pytest\n\npytestmark = pytest.mark.xdist_group('shared-port')\n")
    seconds = {"a": 1, "b": 2, "c": 3, "d": 5, "e": 7, "f": 9}
    (tmp_path / ".test_durations").write_text(
        json.dumps({f"tests/test_{name}.py::t": value for name, value in seconds.items()} | {"tests/x.py::t": 50})
    )
    parts = [part_files(tmp_path, list(reversed(files)), (index, total)) for index in range(total)]
    assert sorted(path for part in parts for path in part) == files
    assert all(part == sorted(part) for part in parts)
    loads = [sum(seconds[path[11]] for path in part) for part in parts]
    expected = {1: [27], 2: [14, 13], 3: [9, 9, 9], 6: [9, 7, 5, 3, 2, 1]}[total]
    assert loads == expected
    assert part_files(tmp_path, files[:1], (2, 3)) == []


def test_statistics_split_reads_each_duration_a_bounded_number_of_times():
    from scripts.ci_mutation.selection import split_parts

    class Counted(dict):
        reads = 0

        def __getitem__(self, key):
            self.reads += 1
            return super().__getitem__(key)

    seconds = Counted({f"tests/test_x.py::test_{index}": float(index % 7) for index in range(400)})
    split_parts(list(seconds), seconds, (0, 4))
    assert seconds.reads <= 2 * len(seconds)


def test_statistics_split_places_equal_durations_by_name_into_the_first_lightest_part():
    from scripts.ci_mutation.selection import partition

    files = [f"tests/test_{name}.py" for name in "dcba"]
    seconds = dict.fromkeys(files, 1.0)
    assert partition(files, seconds, 2) == [
        ["tests/test_a.py", "tests/test_c.py"],
        ["tests/test_b.py", "tests/test_d.py"],
    ]


def test_statistics_parts_split_one_file_without_losing_new_parameter_cases(tmp_path, monkeypatch):
    from scripts.ci_mutation.selection import load_or_collect_stats

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    (tmp_path / "tests").mkdir()
    test = "tests/test_sample.py"
    (tmp_path / test).write_text(
        "import pytest\n"
        "def test_slow(): pass\n"
        "def test_middle(): pass\n"
        "def test_short(): pass\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_new(value): pass\n"
    )
    recorded = {f"{test}::test_{name}": seconds for name, seconds in [("slow", 100), ("middle", 60), ("short", 30)]}
    (tmp_path / ".test_durations").write_text(json.dumps(recorded))
    config = SimpleNamespace(source_paths=[Path("scripts")], pytest_add_cli_args_test_selection=[test])
    runner = SimpleNamespace(
        Config=SimpleNamespace(get=lambda: config),
        SourceFileMutationData=lambda **kwargs: SimpleNamespace(load=lambda: None, exit_code_by_key={"m": None}),
    )
    parts = []

    def collect(engine, test_runner, files, workers, work):
        parts.append(files)
        return [{"status": 0, "tests": {"sample": files}, "durations": {}, "cpu": 0}]

    monkeypatch.setattr("scripts.ci_mutation.selection.collect_file_stats", collect)
    for index in range(2):
        output = tmp_path / f"part-{index}.json"
        with pytest.raises(SystemExit) as done:
            load_or_collect_stats(
                runner,
                SimpleNamespace(_pytest_add_cli_args=[]),
                ["scripts/sample.py"],
                "collect",
                (str(output), "key", str(index), "2"),
            )
        assert done.value.code == 0
        assert json.loads(output.read_text())["results"][0]["tests"]["sample"] == parts[index]
        assert config.source_paths == [Path("scripts")]
    assert parts == [
        [f"{test}::test_slow"],
        [f"{test}::test_middle", f"{test}::test_new[1]", f"{test}::test_new[2]", f"{test}::test_short"],
    ]


def test_statistics_collection_filters_wall_clock_and_rejects_collection_errors(tmp_path, monkeypatch):
    from scripts.ci_mutation.selection import collect_test_ids

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    test = "test_sample.py"
    (tmp_path / test).write_text("import pytest\n@pytest.mark.wall_clock\ndef test_bounded(): pass\n")
    runner = SimpleNamespace(_pytest_add_cli_args=[])
    assert collect_test_ids(runner, [test]) == []
    with pytest.raises(SystemExit) as failed:
        collect_test_ids(runner, [f"{test}::test_missing"])
    assert failed.value.code == 1


@pytest.mark.parametrize("override", [False, True])
def test_statistics_timings_keep_maxima_and_normalize_groups_from_each_cache_location(tmp_path, monkeypatch, override):
    from scripts.ci_mutation.selection import file_seconds, recorded_seconds, traced_records, unit_records

    test = "tests/test_sample.py::test_value"
    (tmp_path / ".test_durations").write_text(json.dumps({f"{test}@port": 0.75}))
    cache = tmp_path / ("external" if override else ".mutation-stats-cache")
    cache.mkdir()
    if override:
        monkeypatch.setenv("MUTATION_STATS_CACHE", str(cache))
    else:
        monkeypatch.delenv("MUTATION_STATS_CACHE", raising=False)
    (cache / "success.json").write_text(
        json.dumps({"status": 0, "durations": {test: 0.25, "tests/test_sample.py::test_cached": 0.5}})
    )
    (cache / "failed.json").write_text(json.dumps({"status": 1, "durations": {test: 20}}))
    assert recorded_seconds([*unit_records(tmp_path), *traced_records(tmp_path)]) == {
        test: 0.75,
        "tests/test_sample.py::test_cached": 0.5,
    }
    assert file_seconds(tmp_path, [test, "tests/test_new.py"], split=True) == {
        test: 0.76,
        "tests/test_new.py": 0.01,
    }


def test_statistics_parts_partition_every_test_when_each_part_restores_a_different_cache(tmp_path, monkeypatch):
    from scripts.ci_mutation.selection import part_files

    tests = [f"tests/test_sample.py::test_{name}" for name in "abc"]
    (tmp_path / ".test_durations").write_text(json.dumps(dict(zip(tests, [3, 2, 1]))))
    caches = [tmp_path / "part-0", tmp_path / "part-1"]
    for cache in caches:
        cache.mkdir()
    (caches[0] / "slow.json").write_text(json.dumps({"status": 0, "durations": {tests[2]: 10}}))
    parts = []
    for index, cache in enumerate(caches):
        monkeypatch.setenv("MUTATION_STATS_CACHE", str(cache))
        parts.append(part_files(tmp_path, tests, (index, 2)))
    assert parts == [[tests[0]], tests[1:]]


def test_statistics_collection_sets_the_mutmut_mode_and_preserves_runner_options(tmp_path, monkeypatch):
    from scripts.ci_mutation.selection import collect_test_ids

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    parent = os.environ.get("MUTANT_UNDER_TEST", "parent")
    monkeypatch.setenv("MUTANT_UNDER_TEST", parent)
    test = "test_mode.py"
    (tmp_path / test).write_text(
        "import os\nassert os.environ['MUTANT_UNDER_TEST'] == 'stats'\ndef test_keep(): pass\ndef test_omit(): pass\n"
    )
    runner = SimpleNamespace(_pytest_add_cli_args=["-k", "keep"])
    assert collect_test_ids(runner, [test]) == [f"{test}::test_keep"]
    assert os.environ["MUTANT_UNDER_TEST"] == parent


def test_statistics_collection_exceptions_exit_the_fork_child_with_failure(tmp_path, monkeypatch):
    from scripts.ci_mutation.selection import collect_test_ids

    class Process:
        def __init__(self, *, target):
            self.target = target
            self.exitcode = None

        def start(self):
            try:
                self.target()
            except SystemExit as error:
                self.exitcode = error.code

        def join(self):
            pass

    def context(method):
        assert method == "fork"
        return SimpleNamespace(Process=Process)

    def fail(*args, **kwargs):
        raise RuntimeError("collection failed")

    def exit_child(code):
        raise SystemExit(code)

    monkeypatch.setenv("MUTANT_UNDER_TEST", os.environ.get("MUTANT_UNDER_TEST", "parent"))
    monkeypatch.setattr("multiprocessing.get_context", context)
    monkeypatch.setattr(pytest, "main", fail)
    monkeypatch.setattr("os._exit", exit_child)
    with pytest.raises(SystemExit) as failed:
        collect_test_ids(SimpleNamespace(_pytest_add_cli_args=[]), ["test_mode.py"])
    assert failed.value.code == 1


def test_shard_stats_run_in_stats_mode_with_their_own_basetemp_and_record_everything(tmp_path, monkeypatch):
    from time import process_time

    from scripts.ci_mutation.selection import collect_shard_stats

    monkeypatch.setenv("MUTANT_UNDER_TEST", os.environ.get("MUTANT_UNDER_TEST", ""))
    monkeypatch.setenv("PY_IGNORE_IMPORTMISMATCH", "0")
    work, root = tmp_path / "work", tmp_path / "root"
    (work / "mutants/scripts").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    monkeypatch.chdir(work)
    monkeypatch.setenv("MUTATION_STATS_ROOT", str(root))
    monkeypatch.setitem(sys.modules, "fake_mutated", SimpleNamespace(__file__=str(work / "mutants/scripts/m.py")))
    monkeypatch.setitem(sys.modules, "fake_rooted", SimpleNamespace(__file__=str(root / "scripts/r.py")))
    engine = SimpleNamespace(tests_by_mangled_function_name={"m.x_f": {"b::t", "a::t"}}, duration_by_test={"a::t": 2.5})
    calls = []

    class Runner:
        _pytest_add_cli_args = ["-q"]

        def run_stats(self, *, tests):
            calls.append(
                (
                    tests,
                    self._pytest_add_cli_args,
                    os.environ["MUTANT_UNDER_TEST"],
                    os.environ["PY_IGNORE_IMPORTMISMATCH"],
                )
            )
            return 4

    output = tmp_path / "out.json"
    collect_shard_stats(SimpleNamespace(mutmut=engine), Runner(), ["tests/test_a.py"], output, "/scratch/base")
    assert calls == [
        (
            ["tests/test_a.py"],
            ["-q", "-p", "pytest_rerunfailures", "--reruns", "1", "-m", "not wall_clock", "--basetemp=/scratch/base"],
            "stats",
            "1",
        )
    ]
    result = json.loads(output.read_text())
    assert 0 <= result.pop("cpu") <= process_time()
    modules = result.pop("modules")
    assert modules == sorted(modules) and {"scripts/m.py", "scripts/r.py"} <= set(modules)
    assert not any(name.startswith("mutants/") for name in modules)
    assert result == {
        "status": 4,
        "tests": {"m.x_f": ["a::t", "b::t"]},
        "durations": {"a::t": 2.5},
        "files": ["tests/test_a.py"],
    }


WALL_CLOCK_SPLITS = {
    "tests/swarm_ledger/test_hook.py": [("test_four_stops_answer_within_two_seconds", "test_block_budget_then_allow")],
    "tests/test_hook_targets.py": [("test_deep_history_stays_fast", "test_deep_history_resolves_the_newest_rollout")],
    "tests/swarm_ledger/test_hub.py": [
        ("test_wait_wakes_at_once_on_a_publish", "test_wait_wakes_on_a_publish"),
        ("test_wait_answers_at_once_when_events_are_already_kept", "test_wait_answers_when_events_are_already_kept"),
    ],
    "tests/gates/test_prompts.py": [
        (
            "test_inline_scripts_read_a_long_option_word_in_linear_time",
            "test_inline_scripts_find_no_script_in_a_long_option_word",
        )
    ],
    "tests/swarm/test_store.py": [
        (
            "test_an_unreachable_redis_is_refused_within_a_second",
            "test_an_unreachable_redis_is_refused_with_a_clear_error",
        ),
        ("test_the_suite_swarm_redis_is_refused_at_once", "test_the_suite_swarm_redis_is_refused"),
    ],
    "tests/swarm_ledger/test_server_teardown.py": [
        ("test_the_test_server_stops_without_waiting_half_a_second", "test_the_test_server_starts_and_stops")
    ],
    "tests/swarm_ledger/test_server_ensure.py": [
        (
            "test_occupied_unresponsive_port_times_out_without_starting",
            "test_occupied_unresponsive_port_gives_up_without_starting",
        )
    ],
    "tests/observability/test_trace_flush.py": [
        ("test_an_attempt_is_killed_within_seconds_of_its_timeout", "test_an_attempt_is_killed_at_its_timeout"),
        ("test_slow_endpoint_is_bounded_per_attempt", "test_slow_endpoint_is_retried_later"),
    ],
    "tests/hive/test_cli.py": [
        ("test_a_stalled_tls_client_is_dropped_after_the_request_timeout", "test_a_stalled_tls_client_is_dropped")
    ],
    "tests/swarm/test_cli.py": [
        (
            "test_a_quick_swarm_keeps_its_minute_while_a_slow_one_runs",
            "test_a_quick_swarm_keeps_ticking_while_a_slow_one_runs",
        )
    ],
    "tests/gates/test_push_stop_executor.py": [
        (
            "test_a_pre_push_gate_that_overruns_is_stopped_by_the_gate_inside_the_stop_condition",
            "test_a_pre_push_gate_that_overruns_is_stopped_inside_the_stop_condition",
        )
    ],
}


@pytest.mark.parametrize("path", sorted(WALL_CLOCK_SPLITS))
def test_only_the_timing_half_of_each_budget_test_carries_the_wall_clock_marker(path):
    root = Path(__file__).resolve().parents[1]
    markers = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["markers"]
    assert any(marker.startswith("wall_clock:") for marker in markers)
    decorators = {
        node.name: {ast.unparse(decorator) for decorator in node.decorator_list}
        for node in ast.walk(ast.parse((root / path).read_text()))
        if isinstance(node, ast.FunctionDef)
    }
    for timed, functional in WALL_CLOCK_SPLITS[path]:
        assert "pytest.mark.wall_clock" in decorators[timed], timed
        assert "pytest.mark.wall_clock" not in decorators[functional], functional


def test_parallel_stats_merge_every_shard_and_fail_on_any_red_shard(tmp_path, capsys):
    from collections import defaultdict

    from scripts.ci_mutation.selection import collect_parallel_stats

    saved = []
    environment = os.environ.get("MUTANT_UNDER_TEST")
    engine = SimpleNamespace(tests_by_mangled_function_name=defaultdict(set), duration_by_test={}, stats_time=None)
    runner = SimpleNamespace(
        mutmut=engine, save_stats=lambda: saved.append(dict(engine.tests_by_mangled_function_name))
    )

    class TestRunner:
        _pytest_add_cli_args = ["-q"]

        def run_stats(self, *, tests):
            assert os.environ["MUTANT_UNDER_TEST"] == "stats"
            basetemp = Path(self._pytest_add_cli_args[-1].removeprefix("--basetemp="))
            assert basetemp.is_dir()
            assert basetemp.name.startswith("mutation-stats-")
            for test in tests:
                engine.tests_by_mangled_function_name["m.x_f"].add(f"{test}::t")
                engine.duration_by_test[f"{test}::t"] = 1.5
            if "tests/test_boom.py" in tests:
                raise RuntimeError("boom")
            return 3 if "tests/test_red.py" in tests else 0

    collect_parallel_stats(runner, TestRunner(), [["tests/test_a.py"], ["tests/test_b.py"]], tmp_path)
    assert os.environ.get("MUTANT_UNDER_TEST") == environment
    assert engine.tests_by_mangled_function_name == {"m.x_f": {"tests/test_a.py::t", "tests/test_b.py::t"}}
    assert engine.duration_by_test == {"tests/test_a.py::t": 1.5, "tests/test_b.py::t": 1.5}
    assert 0 <= engine.stats_time < 5
    assert saved == [{"m.x_f": {"tests/test_a.py::t", "tests/test_b.py::t"}}]
    for shards, status in (([["tests/test_a.py"], ["tests/test_red.py"]], "[3]"), ([["tests/test_boom.py"]], "[1]")):
        with pytest.raises(SystemExit) as failed:
            collect_parallel_stats(runner, TestRunner(), shards, tmp_path)
        assert failed.value.code == 1
        assert capsys.readouterr().out.endswith(f"failed to collect stats. runner returned {status}\n")
    engine.tests_by_mangled_function_name.clear()
    TestRunner.run_stats = lambda self, *, tests: 0
    with pytest.raises(SystemExit) as empty:
        collect_parallel_stats(runner, TestRunner(), [["tests/test_a.py"]], tmp_path)
    assert empty.value.code == 1
    assert capsys.readouterr().out == "failed to collect stats: no selected test reaches a mutated function\n"
    assert len(saved) == 1


def _gate_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks/__init__.py").touch()
    (tmp_path / "scripts/__init__.py").touch()
    shutil.copytree(
        Path(__file__).parents[1] / "scripts/ci_mutation",
        tmp_path / "scripts/ci_mutation",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")


@pytest.mark.parametrize("failures", [1, 2])
def test_statistics_retry_a_flake_once_and_reject_a_persistent_failure(tmp_path, monkeypatch, failures):
    _gate_tree(tmp_path, monkeypatch)
    attempts = tmp_path / "attempts"
    (tmp_path / "scripts/sample.py").write_text("def covered(value):\n    return value + 1\n")
    (tmp_path / "tests/test_sample.py").write_text(
        "import os\nfrom pathlib import Path\nfrom scripts.sample import covered\n\n"
        "def test_value():\n"
        "    assert covered(1) == 2\n"
        "    if os.environ.get('MUTANT_UNDER_TEST') == 'stats':\n"
        f"        attempts = Path({str(attempts)!r})\n"
        "        count = int(attempts.read_text()) + 1 if attempts.exists() else 1\n"
        "        attempts.write_text(str(count))\n"
        f"        assert count > {failures}\n"
    )
    report = run_gate(tmp_path, {"scripts/sample.py": {2}}, tmp_path / "evidence", 60)
    assert attempts.read_text() == "2"
    assert report["failed"] is (failures == 2)
    if failures == 1:
        assert report["not_mutated"] == []
        assert report["files"][0]["counts"] == {"killed": 2}


@pytest.mark.parametrize("source", ["hooks/__init__.py", "scripts/sample/__init__.py"])
def test_gate_kills_initializer_mutants_through_ordinary_package_imports(tmp_path, monkeypatch, source):
    _gate_tree(tmp_path, monkeypatch)
    initializer = tmp_path / source
    initializer.parent.mkdir(exist_ok=True)
    initializer.write_text("def value(number):\n    return number + 1\n")
    module = "hooks" if source == "hooks/__init__.py" else "scripts.sample"
    imported = "import hooks as package" if module == "hooks" else "from scripts import sample as package"
    (tmp_path / "tests/test_ordinary.py").write_text(
        "import importlib\nimport sys\n\n"
        f"{imported}\n\n"
        "def test_package():\n"
        f"    assert package is importlib.import_module('{module}')\n"
        "    assert package.__name__ + '.__init__' not in sys.modules\n"
        "    assert package.value(1) == 2\n"
    )
    report = run_gate(tmp_path, {source: {2}}, tmp_path / "evidence", 60)
    assert report["not_mutated"] == []
    assert report["failed"] is False
    assert report["files"][0]["counts"] == {"killed": 2}


def test_gate_runs_one_collection_per_change(tmp_path, monkeypatch):
    _gate_tree(tmp_path, monkeypatch)
    (tmp_path / "scripts/first.py").write_text("def one(value):\n    return value + 1\n")
    (tmp_path / "scripts/second.py").write_text("def two(value):\n    return value + 2\n")
    (tmp_path / "tests/test_first.py").write_text(
        "from scripts.first import one\n\ndef test_one():\n    assert one(1) == 2\n"
    )
    (tmp_path / "tests/test_second.py").write_text(
        "from scripts.second import two\n\ndef test_two():\n    assert two(1) == 3\n"
    )
    report = run_gate(tmp_path, {"scripts/first.py": {2}, "scripts/second.py": {2}}, tmp_path / "evidence", 60)
    assert report["not_mutated"] == []
    assert [result["path"] for result in report["files"]] == ["scripts/first.py", "scripts/second.py"]
    assert all(result["counts"] == {"killed": 2} for result in report["files"])
    assert len([path for path in (tmp_path / "evidence").iterdir() if path.is_dir()]) == 1


def test_gate_mutates_a_function_its_module_calls_at_import(tmp_path, monkeypatch):
    _gate_tree(tmp_path, monkeypatch)
    (tmp_path / "scripts/sample.py").write_text("def value(number):\n    return number + 1\n\nLOADED = value(1)\n")
    (tmp_path / "tests/test_sample.py").write_text(
        "from scripts.sample import LOADED, value\n\ndef test_value():\n    assert LOADED == 2\n    assert value(1) == 2\n"
    )
    report = run_gate(tmp_path, {"scripts/sample.py": {2}}, tmp_path / "evidence", 60)
    assert report["not_mutated"] == []
    assert report["failed"] is False
    assert report["files"][0]["counts"] == {"killed": 2}


def test_a_subprocess_importing_a_mutated_module_skips_the_mutation_engine_and_still_kills(tmp_path, monkeypatch):
    _gate_tree(tmp_path, monkeypatch)
    (tmp_path / "scripts/sample.py").write_text("def value(number):\n    return number + 1\n")
    child = (
        "import sys\n"
        "from scripts.sample import value\n"
        "print(value(1), 'libcst' in sys.modules)\n"
        "print(callable(__import__('mutmut.__main__').__main__.walk_all_files))\n"
    )
    (tmp_path / "tests/test_sample.py").write_text(
        "import os, subprocess, sys\n\n"
        "from scripts.sample import value\n\n"
        "def test_value():\n"
        "    assert sys.modules['mutmut.__main__'].__file__.endswith('mutmut/__main__.py')\n"
        "    value(1)\n"
        "    env = {**os.environ, 'PYTHONPATH': os.getcwd()}\n"
        f"    out = subprocess.run([sys.executable, '-c', {child!r}], env=env, capture_output=True, text=True, check=True)\n"
        "    assert out.stdout.split() == ['2', 'False', 'True']\n"
    )
    report = run_gate(tmp_path, {"scripts/sample.py": {2}}, tmp_path / "evidence", 60)
    logs = [Path(item["reason"].rpartition(" ")[2]) for item in report["not_mutated"]]
    assert report["not_mutated"] == [], [log.read_text()[-4000:] for log in logs if log.is_file()]
    assert report["failed"] is False
    assert report["files"][0]["counts"] == {"killed": 2}


def test_light_main_answers_the_trampoline_and_hands_every_other_name_to_the_real_module(monkeypatch):
    from types import ModuleType

    import mutmut.__main__ as real

    from scripts.ci_mutation.selection import LIGHT_MAIN

    light_main = ModuleType("mutmut.__main__")
    exec(LIGHT_MAIN, light_main.__dict__)
    assert issubclass(light_main.MutmutProgrammaticFailException, Exception)
    assert light_main.mangled_name_from_mutant_name("scripts.a.x_f__mutmut_3") == "scripts.a.x_f"
    with pytest.raises(AssertionError, match="^scripts.a.x_f$"):
        light_main.mangled_name_from_mutant_name("scripts.a.x_f")
    assert light_main.record_trampoline_hit("scripts.a.x_f") is None
    with pytest.raises(AttributeError, match="^__path__$"):
        light_main.__path__
    monkeypatch.setattr(sys.modules["mutmut"], "__main__", real)
    monkeypatch.setitem(sys.modules, "mutmut.__main__", light_main)
    assert light_main.walk_all_files.__module__ == "mutmut.__main__"
    loaded = sys.modules["mutmut.__main__"]
    assert loaded is not light_main
    assert loaded.__file__ == real.__file__
    assert light_main.walk_all_files is loaded.walk_all_files
    assert sys.modules["mutmut.__main__"] is loaded


def test_the_mutation_runner_itself_never_takes_the_light_main(tmp_path):
    from scripts.ci_mutation.selection import LIGHT_BOOTSTRAP

    package = tmp_path / "scripts/ci_mutation"
    package.mkdir(parents=True)
    (tmp_path / "scripts/__init__.py").touch()
    (package / "__init__.py").touch()
    report = (
        "import sys\n"
        "main = sys.modules.get('mutmut.__main__')\n"
        "print('absent' if main is None else getattr(main, '__file__', 'light'))\n"
    )
    for path in ("scripts/ci_mutation/selection.py", "scripts/other.py"):
        (tmp_path / path).write_text(LIGHT_BOOTSTRAP + report)
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}

    def loaded(*command):
        return subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, check=True).stdout

    assert loaded(sys.executable, "-m", "scripts.ci_mutation.selection") == "absent\n"
    assert loaded(sys.executable, "-c", "import scripts.other") == "light\n"


def test_shared_run_grades_each_file_only_by_the_tests_selected_for_it(tmp_path, monkeypatch):
    _gate_tree(tmp_path, monkeypatch)
    (tmp_path / "scripts/first.py").write_text("def one(value):\n    return value + 1\n")
    (tmp_path / "scripts/second.py").write_text("def two(value):\n    return value + 2\n")
    (tmp_path / "tests/test_first.py").write_text("def test_nothing():\n    assert True\n")
    (tmp_path / "tests/test_second.py").write_text(
        "import importlib\n\nfrom scripts.second import two\n\n"
        "def test_two():\n    assert two(1) == 3\n"
        "    assert importlib.import_module('scripts.fi' + 'rst').one(1) == 2\n"
    )
    report = run_gate(tmp_path, {"scripts/first.py": {2}, "scripts/second.py": {2}}, tmp_path / "evidence", 60)
    first, second = report["files"]
    assert report["not_mutated"] == []
    assert first["counts"] == {"no tests": 2}
    assert second["counts"] == {"killed": 2}


def test_clean_and_fault_controls_skip_tests_that_reach_no_selected_mutant(tmp_path, monkeypatch):
    _gate_tree(tmp_path, monkeypatch)
    runs = tmp_path / "runs.txt"
    (tmp_path / "scripts/sample.py").write_text("def covered(value):\n    return value + 1\n")
    (tmp_path / "tests/test_sample.py").write_text(
        "from pathlib import Path\n\nfrom scripts.sample import covered\n\n"
        f"def test_a_unrelated():\n    with Path({str(runs)!r}).open('a') as stream:\n        stream.write('run\\n')\n\n"
        "def test_value():\n    assert covered(1) == 2\n"
    )
    report = run_gate(tmp_path, {"scripts/sample.py": {2}}, tmp_path / "evidence", 60)
    assert report["not_mutated"] == []
    assert report["files"][0]["counts"] == {"killed": 2}
    assert runs.read_text() == "run\n"


@pytest.mark.parametrize(
    ("ci", "requested", "quota", "expected"),
    [
        (None, 1, None, 1),
        (None, 2, None, 2),
        (None, 20, None, 2),
        ("", 20, None, 2),
        ("true", 1, None, 1),
        ("true", 2, None, 2),
        ("true", 20, None, 20),
        ("true", 16, 3.0, 3),
        ("true", 16, 2.5, 2),
        ("true", 2, 3.0, 2),
        ("true", 16, 0.5, 1),
    ],
)
def test_local_selection_caps_mutation_and_stats_workers(tmp_path, monkeypatch, ci, requested, quota, expected, capsys):
    from scripts.ci_mutation.selection import run_selected

    monkeypatch.setattr("scripts.ci_mutation.selection.quota_cpus", lambda: quota)
    if ci is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci)
    monkeypatch.setattr(os, "cpu_count", lambda: requested)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(requested)))
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"scripts/sample.py": {"lines": [2], "tests": ["tests/test_sample.py"]}}))
    counts = []
    config = SimpleNamespace(source_paths=[], pytest_add_cli_args_test_selection=[])
    data = SimpleNamespace(exit_code_by_key={"selected": None}, load=lambda: None)
    engine = SimpleNamespace(tests_by_mangled_function_name={})
    runner = SimpleNamespace(
        collect_source_file_mutation_data=None,
        SourceFileMutationData=lambda **kwargs: data,
        Config=SimpleNamespace(get=lambda: config),
        PytestRunner=type("PytestRunner", (), {"run_tests": lambda *args, **kwargs: 0, "execute_pytest": _no_pytest}),
        mutmut=engine,
    )

    def shards(root, tests, count):
        counts.append(count)
        return []

    def cli(args):
        assert args == ["run", "--max-children", str(expected)]
        runner.collect_or_load_stats(object())

    runner.cli = cli
    monkeypatch.setattr("scripts.ci_mutation.selection.stats_shards", shards)
    monkeypatch.setattr("scripts.ci_mutation.selection.collect_parallel_stats", lambda *args: None)
    monkeypatch.setitem(sys.modules, "mutmut", SimpleNamespace(__main__=runner))
    for name in [name for name in sys.modules if name == "scripts" or name.startswith("scripts.")]:
        monkeypatch.setitem(sys.modules, name, sys.modules[name])
    run_selected(selection, (0, 1))
    assert counts == [expected]
    output = capsys.readouterr().out
    assert output == ("" if ci else "Local mutation worker cap: 2 (mutation and stats)\n")
