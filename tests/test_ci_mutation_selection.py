import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ci_mutation.runner import run_gate


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


@pytest.mark.parametrize(
    "header", ["", '"""sample contract"""\n', '"""sample contract"""\nfrom __future__ import annotations\n']
)
def test_selection_passes_exact_lines_before_generation_and_reloads_source_packages(tmp_path, monkeypatch, header):
    from mutmut import configuration as engine_config

    from scripts.ci_mutation.selection import run_selected

    monkeypatch.setattr(engine_config, "_config", None)
    project = tmp_path / "mutants"
    (project / "scripts").mkdir(parents=True)
    (project / "hooks").mkdir()
    (project / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["hooks/"]\n')
    monkeypatch.setattr("os.cpu_count", lambda: 6)
    selection = tmp_path / "lines.json"
    selection.write_text(
        json.dumps(
            {
                "scripts/sample.py": {"lines": [2, 5], "tests": ["tests/test_sample.py"]},
                "hooks/other.py": {"lines": [], "tests": ["tests/test_other.py"]},
            }
        )
    )
    monkeypatch.chdir(tmp_path)
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

    def shard(root, files, durations, count):
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
    runner = SimpleNamespace(
        collect_or_load_stats=None,
        SourceFileMutationData=mutation_data,
        Config=SimpleNamespace(get=lambda: config),
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
        assert runner.PytestRunner().run_tests(mutant_name=None, tests=[]) == 0
        assert runner.collect_or_load_stats(test_runner) is None
        assert collected == [True]
        assert config.source_paths == [Path("hooks/")]
        assert engine.tests_by_mangled_function_name == {
            "scripts.sample.x_f": {"tests/test_sample.py::test_slow"},
            "scripts.sample.x_g": {"tests/test_sample.py::test_fast", "tests/test_sample.py::TestCase::test_m"},
            "hooks.other.x_h": {"tests/test_other.py::test_o"},
            "scripts.unselected.x_u": set(),
        }
        assert control("", []) == 0
        assert control("fail", []) == 0
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
        run_selected(selection)
    assert error.value.code == 7


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
    from scripts.ci_mutation.selection import stats_shards

    (tmp_path / "tests").mkdir()
    files = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/test_d.py", "tests/test_e.py"]
    for path in files:
        (tmp_path / path).write_text("def test_x():\n    pass\n")
    (tmp_path / "tests/test_b.py").write_text('import pytest\n\npytestmark = pytest.mark.xdist_group("redis")\n')
    (tmp_path / "tests/test_d.py").write_text(
        "import pytest\n\n@pytest.mark.xdist_group(name='redis')\ndef test_x(): pass\n"
    )
    durations = {"tests/test_a.py::test_x": 5, "tests/test_c.py::test_x": 4, "tests/test_b.py::t": 1}
    durations |= {"tests/test_d.py::t": 1, "tests/other.py::t": 9}
    shards = stats_shards(tmp_path, files, durations, 3)
    assert sorted(map(sorted, shards)) == [
        ["tests/test_a.py"],
        ["tests/test_b.py", "tests/test_d.py", "tests/test_e.py"],
        ["tests/test_c.py"],
    ]
    assert stats_shards(tmp_path, files[:1], {}, 8) == [["tests/test_a.py"]]


def test_parallel_stats_merge_every_shard_and_fail_on_any_red_shard(tmp_path, capsys):
    from collections import defaultdict

    from scripts.ci_mutation.selection import collect_parallel_stats

    saved = []
    engine = SimpleNamespace(tests_by_mangled_function_name=defaultdict(set), duration_by_test={}, stats_time=None)
    runner = SimpleNamespace(
        mutmut=engine, save_stats=lambda: saved.append(dict(engine.tests_by_mangled_function_name))
    )

    class TestRunner:
        _pytest_add_cli_args = ["-q"]

        def run_stats(self, *, tests):
            assert os.environ["MUTANT_UNDER_TEST"] == "stats"
            assert self._pytest_add_cli_args[0] == "-q"
            assert self._pytest_add_cli_args[1].startswith("--basetemp=")
            for test in tests:
                engine.tests_by_mangled_function_name["m.x_f"].add(f"{test}::t")
                engine.duration_by_test[f"{test}::t"] = 1.5
            if "tests/test_boom.py" in tests:
                raise RuntimeError("boom")
            return 3 if "tests/test_red.py" in tests else 0

    collect_parallel_stats(runner, TestRunner(), [["tests/test_a.py"], ["tests/test_b.py"]], tmp_path)
    assert engine.tests_by_mangled_function_name == {"m.x_f": {"tests/test_a.py::t", "tests/test_b.py::t"}}
    assert engine.duration_by_test == {"tests/test_a.py::t": 1.5, "tests/test_b.py::t": 1.5}
    assert engine.stats_time >= 0
    assert saved == [{"m.x_f": {"tests/test_a.py::t", "tests/test_b.py::t"}}]
    for shards, status in (([["tests/test_a.py"], ["tests/test_red.py"]], "[3]"), ([["tests/test_boom.py"]], "[None]")):
        with pytest.raises(SystemExit) as failed:
            collect_parallel_stats(runner, TestRunner(), shards, tmp_path)
        assert failed.value.code == 1
        assert f"failed to collect stats. runner returned {status}" in capsys.readouterr().out
    engine.tests_by_mangled_function_name.clear()
    TestRunner.run_stats = lambda self, *, tests: 0
    with pytest.raises(SystemExit):
        collect_parallel_stats(runner, TestRunner(), [["tests/test_a.py"]], tmp_path)
    assert "no selected test reaches a mutated function" in capsys.readouterr().out
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
