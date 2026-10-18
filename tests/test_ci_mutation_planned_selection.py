import json
from pathlib import Path

import pytest

from scripts.ci_mutation import plan, runner


@pytest.mark.parametrize("tests,browser", [(["tests/test_sample.py"], "true"), ([], "false")])
def test_plan_publishes_each_file_selection_and_browser_requirement_once(tmp_path, monkeypatch, tests, browser):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    source = "def sample(value):\n    return value + 1\n" if tests else "VALUE = 1\n"
    (tmp_path / "scripts/sample.py").write_text(source)
    (tmp_path / "tests/test_sample.py").write_text("import playwright\n")
    (tmp_path / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["scripts/"]\n')
    output = tmp_path / "output"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr("sys.argv", ["plan"])
    monkeypatch.setattr(plan, "own_bases", lambda *args: ["base"])
    monkeypatch.setattr(plan, "discover_changes", lambda *args: {"scripts/sample.py": {2}})
    calls = []

    def select(root, path):
        calls.append((root, path))
        return tests

    monkeypatch.setattr(plan, "select_tests", select)
    assert plan.main() == 0
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert json.loads((tmp_path / ".mutation-selection.json").read_text()) == {"scripts/sample.py": tests}
    assert values["browser"] == browser
    assert calls == [(tmp_path, Path("scripts/sample.py"))]


@pytest.mark.parametrize("tests", [["tests/test_sample.py"], []])
def test_shard_uses_planned_tests_without_scanning_again(tmp_path, monkeypatch, tests):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/sample.py").write_text("def sample(value):\n    return value + 1\n")
    (tmp_path / ".mutation-selection.json").write_text(json.dumps({"scripts/sample.py": tests}))

    def select(*args):
        raise AssertionError("shard repeated test selection")

    def mutate(root, work, selected, *args):
        assert selected == {"scripts/sample.py": ({2}, tests)}
        return {"scripts/sample.py": []}, ""

    monkeypatch.setattr(runner, "select_tests", select)
    monkeypatch.setattr(runner, "mutate_files", mutate)
    report = runner.run_gate(tmp_path, {"scripts/sample.py": {2}}, tmp_path / "output", 60)
    assert report["failed"] is (not tests)
    if not tests:
        assert report["not_mutated"] == [
            {"path": "scripts/sample.py", "reason": "no matching or importing test modules"}
        ]
