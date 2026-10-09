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

    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(engine_config, "_config", None)
    project = tmp_path / "mutants"
    (project / "scripts").mkdir(parents=True)
    (project / "hooks").mkdir()
    (project / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["hooks/"]\n')
    monkeypatch.setattr("os.cpu_count", lambda: 6)
    monkeypatch.setattr("os.sched_getaffinity", lambda pid: set(range(6)) if pid == 0 else set())
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
        source_paths=[Path("scripts/")], pytest_add_cli_args_test_selection=["tests/test_sample.py"]
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

    def buckets(engine_runner, test_runner, shards, work):
        assert engine_runner is runner
        assert config.source_paths == [tmp_path / "mutants/scripts"]
        seen.append((shards, work))
        return [result]

    def bucket_split(root, files, count):
        assert (root, files) == (tmp_path, ["tests/test_sample.py"])
        return [[f"tests/test_{n}.py"] for n in range(count)]

    class PytestRunner:
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
            assert runner.collect_or_load_stats(object()) is None
            assert seen == []
            assert saved == [5]
            assert engine.tests_by_mangled_function_name == {
                "scripts.sample.x_f": {"tests/test_sample.py::t"},
                "scripts.other.x_g": set(),
            }
            assert engine.duration_by_test == {"tests/test_sample.py::t": 1}
            raise SystemExit(7)
        with pytest.raises(SystemExit) as done:
            runner.collect_or_load_stats(object())
        assert done.value.code == 0
        assert config.source_paths == [Path("scripts/")]
        assert seen == ([] if mode == "empty" else [([["tests/test_1.py"], ["tests/test_4.py"]], tmp_path)])
        assert json.loads(part.read_text()) == {
            "key": "key",
            "part": 1,
            "parts": 3,
            "results": [] if mode == "empty" else [result],
        }
        assert saved == []
        raise SystemExit(7)

    runner.cli = cli
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
    from scripts.ci_mutation.selection import stats_shards

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
    (tmp_path / ".test_durations").unlink()
    assert stats_shards(tmp_path, files, 3) == [
        ["tests/test_b.py", "tests/test_d.py"],
        ["tests/test_a.py", "tests/test_e.py"],
        ["tests/test_c.py", "tests/test_f.py"],
    ]


def test_shard_stats_run_in_stats_mode_with_their_own_basetemp_and_record_everything(tmp_path, monkeypatch):
    from time import process_time

    from scripts.ci_mutation.selection import collect_shard_stats

    monkeypatch.setenv("MUTANT_UNDER_TEST", os.environ.get("MUTANT_UNDER_TEST", ""))
    monkeypatch.setenv("PY_IGNORE_IMPORTMISMATCH", "0")
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
    assert calls == [(["tests/test_a.py"], ["-q", "--basetemp=/scratch/base"], "stats", "1")]
    result = json.loads(output.read_text())
    assert 0 <= result.pop("cpu") <= process_time()
    assert result == {"status": 4, "tests": {"m.x_f": ["a::t", "b::t"]}, "durations": {"a::t": 2.5}}


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
            basetemp = Path(self._pytest_add_cli_args[1].removeprefix("--basetemp="))
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
    ("ci", "requested", "expected"),
    [(None, 1, 1), (None, 2, 2), (None, 20, 2), ("", 20, 2), ("true", 1, 1), ("true", 2, 2), ("true", 20, 20)],
)
def test_local_selection_caps_mutation_and_stats_workers(tmp_path, monkeypatch, ci, requested, expected, capsys):
    from scripts.ci_mutation.selection import run_selected

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
        PytestRunner=type("PytestRunner", (), {"run_tests": lambda *args, **kwargs: 0}),
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
