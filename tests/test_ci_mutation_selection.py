import json
import shutil
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
    shutil.copytree(Path(__file__).parents[1] / "scripts/ci_mutation", tmp_path / "scripts/ci_mutation")
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


def test_selection_passes_exact_lines_before_generation_and_reloads_source_packages(tmp_path, monkeypatch):
    from scripts.ci_mutation.selection import run_selected

    selection = tmp_path / "lines.json"
    selection.write_text(json.dumps({"scripts/sample.py": [2, 5], "hooks/other.py": []}))
    runner = SimpleNamespace(collect_or_load_stats=lambda test_runner: None)
    mutmut = SimpleNamespace(__main__=runner)

    def cli(args):
        assert args == ["run", "--max-children", "1"]
        stream = __import__("io").StringIO()
        names = runner.write_all_mutants_to_file(out=stream, source="source", filename=Path("scripts/sample.py"))
        assert names == ["selected"]
        assert stream.getvalue() == "generated"
        assert calls == [("scripts/sample.py", "source", {2, 5})]
        assert "scripts" not in sys.modules
        assert "scripts.ci_mutation" not in sys.modules
        raise SystemExit(7)

    calls = []

    def selected(filename, source, lines):
        calls.append((filename, source, lines))
        return "generated", ["selected"]

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
