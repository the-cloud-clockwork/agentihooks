import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ci_mutation.runner import run_gate


@pytest.mark.parametrize("lines", [{2}, {2, 5}])
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
    assert result["counts"] == ({"killed": 2, "no tests": 2} if 5 in lines else {"killed": 2})
    assert len(result["failures"]) == (2 if 5 in lines else 0)
    assert all(row["status"] == "no tests" and row["lines"] == [5] for row in result["failures"])
    assert result["untouched_survivors"] == []


def test_selection_passes_exact_lines_before_generation_and_reloads_source_packages(tmp_path, monkeypatch):
    from scripts.ci_mutation.selection import run_selected

    selection = tmp_path / "lines.json"
    selection.write_text(json.dumps({"scripts/sample.py": [2, 5], "hooks/other.py": []}))
    monkeypatch.chdir(tmp_path)
    mutmut = SimpleNamespace(_covered_lines=None)

    def cli(args):
        assert args == ["run", "--max-children", "1"]
        assert mutmut._covered_lines == {
            str(tmp_path / "mutants/scripts/sample.py"): {2, 5},
            str(tmp_path / "mutants/hooks/other.py"): set(),
        }
        assert "scripts" not in sys.modules
        assert "scripts.ci_mutation" not in sys.modules
        raise SystemExit(7)

    monkeypatch.setitem(sys.modules, "mutmut", mutmut)
    monkeypatch.setitem(sys.modules, "mutmut.__main__", SimpleNamespace(cli=cli))
    for name in ("scripts", "scripts.ci_mutation"):
        monkeypatch.setitem(sys.modules, name, sys.modules[name])
    with pytest.raises(SystemExit) as error:
        run_selected(selection)
    assert error.value.code == 7
