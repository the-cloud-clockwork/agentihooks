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


def _recorded_project(tmp_path, monkeypatch):
    import subprocess

    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["scripts/"]\n')
    (tmp_path / "scripts/core.py").write_text(
        "def f(value):\n    return value + 1\n\n\ndef g(value):\n    return value - 1\n"
    )
    (tmp_path / "tests/test_core.py").write_text("from scripts.core import f, g\n")
    (tmp_path / "tests/test_edited.py").write_text("from scripts.core import g\n")
    (tmp_path / "tests/conftest.py").write_text("")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(tmp_path)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "recorded"], check=True)
    commit = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    (tmp_path / "tests/test_fresh.py").write_text("from scripts.core import g\n")
    (tmp_path / "tests/test_edited.py").write_text("from scripts.core import g\n\n\ndef test_new():\n    pass\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "head"], check=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mutmut.configuration._config", None)
    return {
        "commit": commit,
        "units": ["scripts/core.py::f", "scripts/core.py::g", "scripts/core.py::<module>"],
        "tests": {
            "tests/test_core.py::test_f": {"units": [0, 2], "escapes": False},
            "tests/test_core.py::test_g[1]": {"units": [1, 2], "escapes": False},
            "tests/test_core.py::test_g[2]@port": {"units": [1, 2], "escapes": False},
            "tests/test_core.py::TestSpawn::test_child": {"units": [], "escapes": True},
            "tests/test_core.py::test_other": {"units": [2], "escapes": False},
            "tests/test_edited.py::test_old": {"units": [0, 2], "escapes": False},
        },
    }


def test_a_changed_line_selects_only_the_tests_whose_recorded_coverage_reaches_it(tmp_path, monkeypatch):
    manifest = _recorded_project(tmp_path, monkeypatch)
    selection = {}
    _, _, tests = plan.estimate(tmp_path, {"scripts/core.py": {6}}, selection, manifest)
    assert selection == {
        "scripts/core.py": [
            "tests/test_core.py::TestSpawn::test_child",
            "tests/test_core.py::test_g",
            "tests/test_edited.py",
            "tests/test_fresh.py",
        ]
    }
    assert tests == selection["scripts/core.py"]


def test_plan_publishes_covering_test_ids_and_checks_their_files(tmp_path, monkeypatch, capsys):
    manifest = _recorded_project(tmp_path, monkeypatch)
    (tmp_path / "tests/test_core.py").write_text(
        "from scripts.core import f, g\n\n\nclass TestSpawn:\n    def test_child(self):\n        pass\n"
    )
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setattr("sys.argv", ["plan", "--repository", "owner/repo"])
    monkeypatch.setattr(plan, "own_bases", lambda *args: ["base"])
    monkeypatch.setattr(plan, "discover_changes", lambda *args: {"scripts/core.py": {6}})
    monkeypatch.setattr(plan, "find_record", lambda *args: ({"id": 7}, manifest))
    assert plan.main() == 0
    assert json.loads((tmp_path / ".mutation-selection.json").read_text()) == {
        "scripts/core.py": [
            "tests/test_core.py::TestSpawn::test_child",
            "tests/test_core.py::test_g",
            "tests/test_edited.py",
            "tests/test_fresh.py",
        ]
    }
    assert "browser=false" in (tmp_path / "output").read_text().splitlines()


@pytest.mark.parametrize(
    "change,helper",
    [({"scripts/core.py": {6}}, True), ({"scripts/core.py": {10}}, False)],
)
def test_selection_keeps_whole_files_where_recorded_coverage_cannot_speak(tmp_path, monkeypatch, change, helper):
    manifest = _recorded_project(tmp_path, monkeypatch)
    if helper:
        (tmp_path / "tests/conftest.py").write_text("X = 1\n")
    else:
        (tmp_path / "scripts/core.py").write_text(
            (tmp_path / "scripts/core.py").read_text() + "\n\ndef h(value):\n    return value * 2\n"
        )
    import subprocess

    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(tmp_path), "commit", "-qam", "more"],
        check=True,
    )
    selection = {}
    plan.estimate(tmp_path, change, selection, manifest)
    assert selection == {"scripts/core.py": ["tests/test_core.py", "tests/test_edited.py", "tests/test_fresh.py"]}


def test_estimate_without_a_manifest_commit_keeps_whole_files(tmp_path, monkeypatch):
    manifest = _recorded_project(tmp_path, monkeypatch)
    del manifest["commit"]
    selection = {}
    plan.estimate(tmp_path, {"scripts/core.py": {6}}, selection, manifest)
    assert selection == {"scripts/core.py": ["tests/test_core.py", "tests/test_edited.py", "tests/test_fresh.py"]}
