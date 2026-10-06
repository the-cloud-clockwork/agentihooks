import json
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


def test_selection_passes_exact_lines_before_generation_and_reloads_source_packages(tmp_path, monkeypatch):
    from mutmut import configuration as engine_config

    from scripts.ci_mutation.selection import run_selected

    monkeypatch.setattr(engine_config, "_config", None)
    project = tmp_path / "mutants"
    (project / "scripts").mkdir(parents=True)
    (project / "hooks").mkdir()
    (project / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["hooks/"]\n')
    monkeypatch.setattr("os.cpu_count", lambda: 6)
    selection = tmp_path / "lines.json"
    selection.write_text(json.dumps({"scripts/sample.py": [2, 5], "hooks/other.py": []}))
    test_runner = object()
    data = SimpleNamespace(exit_code_by_key={"selected": None})
    loaded = []

    def load():
        loaded.append(True)

    def mutation_data(*, path):
        assert path in {Path("scripts/sample.py"), Path("hooks/other.py")}
        return data

    config = SimpleNamespace(source_paths=[Path("hooks/")])

    def collect_stats(value):
        assert value is test_runner
        assert loaded
        assert config.source_paths == [Path.cwd() / "mutants/hooks"]
        return "collected"

    data.load = load
    runner = SimpleNamespace(
        collect_or_load_stats=collect_stats,
        SourceFileMutationData=mutation_data,
        Config=SimpleNamespace(get=lambda: config),
    )
    mutmut = SimpleNamespace(__main__=runner)

    def cli(args):
        assert args == ["run", "--max-children", "6"]
        stream = __import__("io").StringIO()
        names = runner.write_all_mutants_to_file(out=stream, source="source", filename=Path("scripts/sample.py"))
        assert names == ["selected"]
        assert stream.getvalue().endswith("generated = True\n")
        namespace = {"__file__": str(project / "scripts/sample.py")}
        cwd = Path.cwd()
        exec(stream.getvalue(), namespace)
        assert namespace["generated"] is True
        assert namespace["__doc__"] == "sample contract"
        assert Path.cwd() == cwd
        assert engine_config.Config.get().source_paths == [project / "hooks"]
        assert calls == [("scripts/sample.py", "source", {2, 5})]
        assert runner.collect_or_load_stats(test_runner) == "collected"
        assert config.source_paths == [Path("hooks/")]
        data.exit_code_by_key = {}
        with pytest.raises(SystemExit) as empty:
            runner.collect_or_load_stats(test_runner)
        assert empty.value.code == 0
        assert "scripts" not in sys.modules
        assert "scripts.ci_mutation" not in sys.modules
        raise SystemExit(7)

    calls = []

    def selected(filename, source, lines):
        calls.append((filename, source, lines))
        return '"""sample contract"""\nfrom __future__ import annotations\ngenerated = True\n', ["selected"]

    monkeypatch.setattr("scripts.ci_mutation.selection.selected_mutants", selected)
    runner.cli = cli
    monkeypatch.setitem(sys.modules, "mutmut", mutmut)
    monkeypatch.setitem(sys.modules, "mutmut.__main__", SimpleNamespace(cli=cli))
    for name in ("scripts", "scripts.ci_mutation"):
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
