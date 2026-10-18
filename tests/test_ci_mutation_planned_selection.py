import json
import time
from datetime import UTC, datetime
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


@pytest.mark.parametrize(
    "recorded,tests,notice",
    [
        (
            True,
            ["tests/test_helper.py"],
            "Recorded coverage from dev run 7 adds the tests that executed a changed function.",
        ),
        (False, [], "No dev test dependency manifest found; tests are selected by import only."),
    ],
)
def test_plan_adds_tests_whose_recorded_coverage_reaches_a_changed_function_through_a_helper(
    tmp_path, monkeypatch, capsys, recorded, tests, notice
):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts/core.py").write_text(
        "class Store:\n    def judge(self, value):\n        return value + 1\n\n\n"
        "def other(value):\n    return value - 1\n"
    )
    (tmp_path / "scripts/helper.py").write_text("from scripts.core import Store\n")
    (tmp_path / "tests/test_helper.py").write_text("from scripts import helper\n")
    (tmp_path / "tests/test_other.py").write_text("import json\n")
    (tmp_path / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["scripts/"]\n')
    manifest = {
        "units": ["scripts/core.py::Store.judge", "scripts/core.py::other", "scripts/core.py::<module>"],
        "tests": {
            "tests/test_helper.py::TestStore::test_judge": {"units": [0]},
            "tests/test_other.py::test_other": {"units": [1, 2]},
            "tests/test_gone.py::test_judge": {"units": [0]},
        },
    }
    asked = []

    def record(repository, before):
        asked.append((repository, datetime.strptime(before, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)))
        return ({"id": 7}, manifest) if recorded else None

    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setattr("sys.argv", ["plan", "--repository", "owner/repo"])
    monkeypatch.setattr("mutmut.configuration._config", None)
    monkeypatch.setattr(plan, "own_bases", lambda *args: ["base"])
    monkeypatch.setattr(plan, "discover_changes", lambda *args: {"scripts/core.py": {3}})
    monkeypatch.setattr(plan, "find_record", record)
    try:
        if tests:
            assert plan.main() == 0
        else:
            with pytest.raises(SystemExit, match="scripts/core.py: not mutated, no matching or importing test modules"):
                plan.main()
    finally:
        monkeypatch.undo()
        time.tzset()
    [(repository, before)] = asked
    assert repository == "owner/repo"
    assert abs((datetime.now(UTC) - before).total_seconds()) < 120
    assert notice in capsys.readouterr().out.splitlines()
    if tests:
        assert json.loads((tmp_path / ".mutation-selection.json").read_text()) == {"scripts/core.py": tests}


def test_refine_grades_the_planned_selection_without_selecting_again(tmp_path, monkeypatch):
    from scripts.ci_mutation import measured

    (tmp_path / "stats").mkdir()
    (tmp_path / ".mutation-selection.json").write_text(
        json.dumps({"scripts/sample.py": ["tests/test_helper.py"], "scripts/unreached.py": []})
    )
    refined = []

    def select(*args):
        raise AssertionError("refine repeated test selection")

    def refine(root, selected, stats):
        refined.append(selected)
        return {"shards": 1, "estimated_worker_seconds": 0.0, "assignments": {}}

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr("sys.argv", ["measured", "--bases", "base", "--stats", str(tmp_path / "stats")])
    monkeypatch.setattr(
        measured, "discover_changes", lambda *args: {"scripts/sample.py": {2}, "scripts/unreached.py": {1}}
    )
    monkeypatch.setattr(measured, "select_tests", select)
    monkeypatch.setattr(measured, "refine_plan", refine)
    monkeypatch.setattr(measured.subprocess, "check_output", lambda *args, **kwargs: "head\n")
    assert measured.main() == 0
    assert refined == [{"scripts/sample.py": ({2}, ["tests/test_helper.py"])}]
