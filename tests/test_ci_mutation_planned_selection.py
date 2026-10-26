import json
import subprocess
import time
import zipfile
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


@pytest.mark.parametrize(
    "failures, error",
    [
        (1, subprocess.CalledProcessError(1, ["gh"], stderr=b"gh: Server Error (HTTP 502)\n")),
        (2, subprocess.CalledProcessError(1, ["gh"], stderr=b"gh: Server Error (HTTP 502)\n")),
        (2, subprocess.TimeoutExpired(["gh"], 60)),
        (2, zipfile.BadZipFile("File is not a zip file")),
        (2, json.JSONDecodeError("Expecting value", "", 0)),
        (2, KeyError("There is no item named 'manifest.json' in the archive")),
    ],
)
def test_plan_retries_an_unreadable_record_once_then_selects_every_test(tmp_path, monkeypatch, capsys, failures, error):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests/nested").mkdir(parents=True)
    (tmp_path / "scripts/core.py").write_text("def judge(value):\n    return value + 1\n")
    (tmp_path / "tests/test_helper.py").write_text("import json\n")
    (tmp_path / "tests/test_core.py").write_text("from scripts.core import judge\n")
    (tmp_path / "tests/nested/test_deep.py").write_text("import os\n")
    (tmp_path / "tests/helpers.py").write_text("import os\n")
    (tmp_path / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["scripts/"]\n')
    manifest = {
        "units": ["scripts/core.py::judge"],
        "tests": {"tests/test_helper.py::test_judge": {"units": [0]}},
    }
    calls, pauses = [], []

    def record(repository, before):
        calls.append((repository, before))
        if len(calls) <= failures:
            raise error
        return {"id": 7}, manifest

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setattr("sys.argv", ["plan", "--repository", "owner/repo"])
    monkeypatch.setattr("mutmut.configuration._config", None)
    monkeypatch.setattr(plan, "own_bases", lambda *args: ["base"])
    monkeypatch.setattr(plan, "discover_changes", lambda *args: {"scripts/core.py": {2}})
    monkeypatch.setattr(plan, "find_record", record)
    monkeypatch.setattr(plan.time, "sleep", pauses.append)
    assert plan.main() == 0
    assert [repository for repository, _ in calls] == ["owner/repo", "owner/repo"]
    assert calls[0][1] == calls[1][1]
    assert pauses == [plan.RECORD_RETRY_SECONDS]
    selected = json.loads((tmp_path / ".mutation-selection.json").read_text())["scripts/core.py"]
    out = capsys.readouterr().out
    reason = plan.record_failure(error)
    assert f"Reading the recorded dev coverage failed on attempt 1: {reason}" in out
    if failures == 1:
        assert selected == ["tests/test_core.py", "tests/test_helper.py"]
        assert "Recorded coverage from dev run 7 adds the tests that executed a changed function." in out
        assert "selection fallback" not in out
    else:
        assert selected == ["tests/nested/test_deep.py", "tests/test_core.py", "tests/test_helper.py"]
        assert f"Reading the recorded dev coverage failed on attempt 2: {reason}" in out
        assert (
            "::warning title=Mutation selection fallback::The recorded dev coverage could not be read, so every"
            f" test module is selected for each changed file: {reason}"
        ) in out.splitlines()


def test_record_failure_names_the_gh_error_text():
    error = subprocess.CalledProcessError(1, ["gh"], stderr=b"gh: Server Error (HTTP 502)\n")
    assert plan.record_failure(error) == "CalledProcessError: gh: Server Error (HTTP 502)"
    assert plan.record_failure(zipfile.BadZipFile("File is not a zip file")) == "BadZipFile: File is not a zip file"
    assert plan.record_failure(KeyError("manifest.json")) == "KeyError: 'manifest.json'"
    garbled = subprocess.CalledProcessError(1, ["gh"], stderr=b"gh: bad \xff byte\n")
    assert plan.record_failure(garbled) == "CalledProcessError: gh: bad � byte"


def test_every_test_selection_passes_the_planner_contracts_on_this_tree():
    root = Path(__file__).resolve().parent.parent
    everything = plan.every_test(root)
    assert set(everything) >= {
        path for source in ("scripts/ci_mutation/plan.py",) for path in plan.select_tests(root, Path(source))
    }
    assert all(Path(path).name.startswith("test_") for path in everything)
    plan.validate_tests(root, everything)


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


GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


def _commit(root, message):
    subprocess.run([*GIT, "-C", str(root), "add", "."], check=True)
    subprocess.run([*GIT, "-C", str(root), "commit", "-q", "-m", message], check=True)
    return subprocess.check_output([*GIT, "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


def _recorded_project(tmp_path, monkeypatch):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["scripts/"]\n')
    (tmp_path / "scripts/core.py").write_text(
        "def f(value):\n    return value + 1\n\n\ndef g(value):\n    return value - 1\n"
    )
    (tmp_path / "tests/test_core.py").write_text(
        "from scripts.core import f, g\n\n\n"
        + "".join(f"def {name}():\n    pass\n\n\n" for name in ("test_f", "test_g", "test_other"))
        + "class TestSpawn:\n    def test_child(self):\n        pass\n"
    )
    (tmp_path / "tests/test_edited.py").write_text("from scripts.core import g\n")
    (tmp_path / "tests/conftest.py").write_text("")
    subprocess.run([*GIT, "-C", str(tmp_path), "init", "-q"], check=True)
    commit = _commit(tmp_path, "recorded")
    (tmp_path / "tests/test_fresh.py").write_text("from scripts.core import g\n\n\ndef test_fresh():\n    pass\n")
    (tmp_path / "tests/test_edited.py").write_text("from scripts.core import g\n\n\ndef test_new():\n    pass\n")
    _commit(tmp_path, "head")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mutmut.configuration._config", None)
    return {
        "commit": commit,
        "units": ["scripts/core.py::f", "scripts/core.py::g", "scripts/core.py::<module>"],
        "tests": {
            "tests/test_core.py::test_f": {"units": [0, 2], "escapes": False},
            "tests/test_core.py::test_g[1]": {"units": [1, 2], "escapes": False},
            "tests/test_core.py::test_g[2]@port": {"units": [1, 2], "escapes": False},
            "tests/test_core.py::TestSpawn::test_child": {"units": [2], "escapes": True},
            "tests/test_core.py::test_other": {"units": [2], "escapes": False},
            "tests/test_edited.py::test_old": {"units": [0, 2], "escapes": False},
        },
    }


NARROWED = ["tests/test_core.py::test_g", "tests/test_edited.py", "tests/test_fresh.py"]
WHOLE = ["tests/test_core.py", "tests/test_edited.py", "tests/test_fresh.py"]


@pytest.mark.parametrize("line", [5, 6])
def test_a_changed_line_selects_only_the_tests_whose_recorded_coverage_reaches_it(tmp_path, monkeypatch, line):
    manifest = _recorded_project(tmp_path, monkeypatch)
    selection = {}
    _, _, tests = plan.estimate(tmp_path, {"scripts/core.py": {line}}, selection, manifest)
    assert selection == {"scripts/core.py": NARROWED}
    assert tests == NARROWED


def _line_project(tmp_path, monkeypatch, mode):
    from coverage import CoverageData

    from tests.dep_manifest import build

    _recorded_project(tmp_path, monkeypatch)
    source = "def f(value):\n    if value:\n        return value + 1\n    return value - 1\n"
    if mode == "repeated":
        source = "def f(value):\n    if value:\n        value += 1\n" + "    pass\n" * 300
    (tmp_path / "scripts/core.py").write_text(source)
    (tmp_path / "scripts/helper.py").write_text("from scripts.core import f\n")
    (tmp_path / "scripts/aaa.py").write_text("def other():\n    return 1\n")
    (tmp_path / "scripts/settings.json").write_text("{}")
    (tmp_path / "tests/test_helper.py").write_text("from scripts.helper import f\n\ndef test_helper():\n    pass\n")
    commit = _commit(tmp_path, "shared function")
    data = CoverageData(basename=str(tmp_path / ".coverage"))
    for test, lines in (
        ("tests/test_core.py::test_f", [2, 3]),
        ("tests/test_core.py::test_g[1]", [2, 4]),
        ("tests/test_core.py::test_other", [2, 3]),
        ("tests/test_core.py::TestSpawn::test_child", [2, 3]),
        ("tests/test_helper.py::test_helper", [2, 4]),
    ):
        if mode == "repeated":
            lines = [2, *range(4, 304)]
            if test.endswith(("test_f", "test_helper")):
                lines.append(3)
        data.set_context(f"{test}|run")
        path = (
            "scripts/aaa.py"
            if mode in {"separate", "deleted", "dependency"} and test.endswith("test_other")
            else "scripts/core.py"
        )
        data.add_lines({str(tmp_path / path): lines if path.endswith("core.py") else [2]})
    data.write()
    manifest = build(tmp_path / ".coverage", [], tmp_path, commit, "test")
    return manifest, source


def _line_contexts(tmp_path, manifest, mode):
    if mode == "legacy":
        del manifest["lines"]
    elif mode == "missing":
        del manifest["lines"]["tests/test_core.py::test_other"]
    elif mode == "parameter":
        manifest["tests"]["tests/test_core.py::test_g[2]"] = {"units": [], "files": [], "escapes": False}
    if mode == "data":
        manifest["tests"]["tests/test_core.py::test_other"]["files"] = ["scripts/settings.json"]
        (tmp_path / "scripts/settings.json").write_text('{"changed": true}')
    if mode == "separate":
        del manifest["tests"]["tests/test_core.py::test_other"]["files"]
    if mode == "new":
        (tmp_path / "scripts/fresh.py").write_text("def fresh():\n    return 1\n")
    elif mode == "deleted":
        (tmp_path / "scripts/aaa.py").unlink()
    elif mode == "dependency":
        (tmp_path / "scripts/aaa.py").write_text("def other():\n    return 2\n")


@pytest.mark.parametrize("shift", [0, 2])
@pytest.mark.parametrize(
    "mode",
    [
        "line",
        "legacy",
        "missing",
        "inserted",
        "header",
        "parameter",
        "drift",
        "separate",
        "data",
        "module",
        "new",
        "deleted",
        "trailing",
        "dependency",
        "repeated",
    ],
)
def test_statistics_select_only_contexts_executing_the_edited_branch(tmp_path, monkeypatch, shift, mode):
    manifest, source = _line_project(tmp_path, monkeypatch, mode)
    _line_contexts(tmp_path, manifest, mode)
    new = source.replace("value - 1", "value - 2")
    if mode == "drift":
        new = source.replace("value + 1", "value + 2")
    elif mode == "module":
        new = source + "\nSETTING = 1\n"
    elif mode == "trailing":
        new = source + "\n"
    elif mode == "repeated":
        new = source.replace("value += 1", "value += 2")
    if mode == "header":
        new = new.replace("def f(value)", "def f(value=1)")
    if mode == "inserted":
        new = new.replace("    return value - 2", "    value += 1\n    return value - 2")
    (tmp_path / "scripts/core.py").write_text("\n" * shift + new)
    _commit(tmp_path, "edited branch")
    selection = {}
    line = 3 if mode == "repeated" else 4
    plan.estimate(tmp_path, {"scripts/core.py": {line + shift}}, selection, manifest)
    expected = ["tests/test_core.py::test_g"]
    if mode in {"legacy", "inserted", "header", "drift"}:
        expected = [
            f"tests/test_core.py::{name}" for name in ("TestSpawn::test_child", "test_f", "test_g", "test_other")
        ]
    elif mode in {"missing", "parameter"}:
        expected = ["tests/test_core.py"]
    elif mode in {"data", "deleted", "dependency"}:
        expected.append("tests/test_core.py::test_other")
    elif mode == "repeated":
        expected = ["tests/test_core.py::test_f"]
    assert selection["scripts/core.py"] == [
        *expected,
        "tests/test_edited.py",
        "tests/test_fresh.py",
        "tests/test_helper.py::test_helper",
    ]


def test_a_changed_signature_adds_only_the_tests_reaching_its_function(tmp_path, monkeypatch):
    from coverage import CoverageData

    from tests.dep_manifest import build

    _recorded_project(tmp_path, monkeypatch)
    source = (
        "def f(value):\n    return value + 1\n\n\n"
        "def g(value):\n    if value:\n        return value + 1\n    return value - 1\n"
    )
    (tmp_path / "scripts/core.py").write_text(source)
    commit = _commit(tmp_path, "two functions")
    data = CoverageData(basename=str(tmp_path / ".coverage"))
    for test, lines in (
        ("tests/test_core.py::test_f", [2]),
        ("tests/test_core.py::test_g", [6, 8]),
        ("tests/test_core.py::test_other", [6, 7]),
        ("tests/test_core.py::TestSpawn::test_child", [6, 7]),
    ):
        data.set_context(f"{test}|run")
        data.add_lines({str(tmp_path / "scripts/core.py"): lines})
    data.write()
    manifest = build(tmp_path / ".coverage", [], tmp_path, commit, "test")
    (tmp_path / "scripts/core.py").write_text(
        source.replace("def f(value)", "def f(value=1)").replace("value - 1", "value - 2")
    )
    _commit(tmp_path, "edited signature")
    selection = {}
    plan.estimate(tmp_path, {"scripts/core.py": {8}}, selection, manifest)
    assert selection["scripts/core.py"] == [
        "tests/test_core.py::test_f",
        "tests/test_core.py::test_g",
        "tests/test_edited.py",
        "tests/test_fresh.py",
    ]


def test_a_changed_nested_signature_adds_the_tests_reaching_the_nested_function(tmp_path, monkeypatch):
    from coverage import CoverageData

    from tests.dep_manifest import build

    _recorded_project(tmp_path, monkeypatch)
    source = (
        "def outer(value):\n    def inner(\n        item,\n    ):\n        return item + 1\n\n"
        "    if value:\n        return inner(value)\n    return value\n"
    )
    (tmp_path / "scripts/core.py").write_text(source)
    commit = _commit(tmp_path, "nested function")
    data = CoverageData(basename=str(tmp_path / ".coverage"))
    for test, lines in (
        ("tests/test_core.py::test_f", [2, 5, 7, 8]),
        ("tests/test_core.py::test_g", [2, 7, 9]),
        ("tests/test_core.py::test_other", [2, 7, 9]),
        ("tests/test_core.py::TestSpawn::test_child", [2, 7, 9]),
    ):
        data.set_context(f"{test}|run")
        data.add_lines({str(tmp_path / "scripts/core.py"): lines})
    data.write()
    manifest = build(tmp_path / ".coverage", [], tmp_path, commit, "test")
    (tmp_path / "scripts/core.py").write_text(source.replace("        item,", "        item=1,"))
    _commit(tmp_path, "edited nested signature")
    selection = {}
    plan.estimate(tmp_path, {"scripts/core.py": {3}}, selection, manifest)
    assert selection["scripts/core.py"] == [
        "tests/test_core.py::test_f",
        "tests/test_edited.py",
        "tests/test_fresh.py",
    ]


@pytest.mark.parametrize(
    ("mode", "line", "expected"),
    [
        ("import", 12, ["tests/test_core.py::test_g"]),
        ("constant", 12, ["tests/test_core.py::test_g"]),
        ("from", 11, ["tests/test_core.py::test_g"]),
        (
            "rebound",
            11,
            [
                "tests/test_core.py::TestSpawn::test_child",
                "tests/test_core.py::test_f",
                "tests/test_core.py::test_g",
                "tests/test_core.py::test_other",
            ],
        ),
    ],
)
def test_a_module_edit_that_only_binds_new_names_keeps_the_selection_narrow(
    tmp_path, monkeypatch, mode, line, expected
):
    from coverage import CoverageData

    from tests.dep_manifest import build

    _recorded_project(tmp_path, monkeypatch)
    source = (
        "from os import path\n\n\n"
        "def f(value):\n    return value + 1\n\n\n"
        "def g(value):\n    if value:\n        return value + 1\n    return value - 1\n"
    )
    (tmp_path / "scripts/core.py").write_text(source)
    commit = _commit(tmp_path, "two functions")
    data = CoverageData(basename=str(tmp_path / ".coverage"))
    for test, lines in (
        ("tests/test_core.py::test_f", [5]),
        ("tests/test_core.py::test_g", [9, 11]),
        ("tests/test_core.py::test_other", [9, 10]),
        ("tests/test_core.py::TestSpawn::test_child", [9, 10]),
    ):
        data.set_context(f"{test}|run")
        data.add_lines({str(tmp_path / "scripts/core.py"): lines})
    data.write()
    manifest = build(tmp_path / ".coverage", [], tmp_path, commit, "test")
    edited = {
        "import": source.replace("from os import path\n", "from os import path\nimport json\n"),
        "constant": source.replace("from os import path\n", "from os import path\nLIMIT = 3\n"),
        "from": source.replace("from os import path\n", "from os import path, sep\n"),
        "rebound": source + "\n\npath = None\n",
    }[mode]
    (tmp_path / "scripts/core.py").write_text(edited.replace("value - 1", "value - 2"))
    _commit(tmp_path, "module edit")
    selection = {}
    plan.estimate(tmp_path, {"scripts/core.py": {line}}, selection, manifest)
    assert selection["scripts/core.py"] == [*expected, "tests/test_edited.py", "tests/test_fresh.py"]


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("def f(:\n", "def f():\n    pass\n", False),
        ("from os import path\n", "from os import path\nimport json\n", True),
        ("X = 1\nfrom os import path\n", "X = 1\nfrom os import path, sep\n", True),
        ("X = 1\nfrom os import path\n", "from os import path, sep\nX = 1\n", False),
        ("A = 1\nB = 2\n", "B = 2\nA = 1\n", False),
        ("A.append(1)\nA.append(2)\n", "A.append(2)\nA.append(1)\n", False),
        ("from sys import sep\n", "from os import path, sep\n", False),
        ("from os import path, sep\n", "from os import sep, path\n", True),
        ("", "from os import sep\n", True),
        ("X = 1\n", "X = 1\nimport json\n", True),
        ("X = 1\n", "X = 1\nprint(X)\n", False),
        ("", "from os import *\n", False),
        ("os = 1\n", "os = 1\nimport os.path\n", False),
        ("y = x\n", "y = x\nx = 1\n", False),
        ("def f():\n    pass\n", "def f():\n    pass\n\n\nf = 1\n", False),
        ("def f():\n    return 1\n", "import json\n\n\ndef f():\n    return 2\n", True),
        ("def f():\n    return 1\n", "@cache\ndef f():\n    return 1\n", False),
        (
            "class A:\n    def m(self):\n        return 1\n",
            "import json\n\n\nclass A:\n    def m(self):\n        return 2\n",
            True,
        ),
        ("class A:\n    pass\n", "class A(B):\n    pass\n", False),
        ("class A:\n    pass\n", "class A(metaclass=M):\n    pass\n", False),
        ("class A:\n    pass\n", "@dataclass\nclass A:\n    pass\n", False),
        ("class A:\n    X = 1\n", "class A:\n    X = 2\n", False),
        ("X = 1\n", "Y = 1\n", False),
    ],
)
def test_only_statements_binding_unknown_names_count_as_new(old, new, expected):
    assert plan.binds_only_new_names(old, new) is expected


def test_plan_publishes_covering_test_ids_and_checks_their_files(tmp_path, monkeypatch):
    manifest = _recorded_project(tmp_path, monkeypatch)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setattr("sys.argv", ["plan", "--repository", "owner/repo"])
    monkeypatch.setattr(plan, "own_bases", lambda *args: ["base"])
    monkeypatch.setattr(plan, "discover_changes", lambda *args: {"scripts/core.py": {6}})
    monkeypatch.setattr(plan, "find_record", lambda *args: ({"id": 7}, manifest))
    assert plan.main() == 0
    assert json.loads((tmp_path / ".mutation-selection.json").read_text()) == {"scripts/core.py": NARROWED}
    assert "browser=false" in (tmp_path / "output").read_text().splitlines()


def test_tests_whose_recorded_code_changed_since_the_record_stay_selected(tmp_path, monkeypatch):
    manifest = _recorded_project(tmp_path, monkeypatch)
    manifest["tests"]["tests/test_core.py::test_other"]["files"] = ["scripts/rules.json"]
    core = tmp_path / "scripts/core.py"
    core.write_text(core.read_text().replace("value + 1", "g(value)"))
    (tmp_path / "scripts/rules.json").write_text("{}\n")
    _commit(tmp_path, "drift")
    selection = {}
    plan.estimate(tmp_path, {"scripts/core.py": {6}}, selection, manifest)
    assert selection == {
        "scripts/core.py": [
            "tests/test_core.py::test_f",
            "tests/test_core.py::test_g",
            "tests/test_core.py::test_other",
            "tests/test_edited.py",
            "tests/test_fresh.py",
        ]
    }


@pytest.mark.parametrize(
    "cause",
    ["helper", "dependency", "root module", "workflow", "new function", "unrecorded test", "edited test", "no commit"],
)
def test_selection_keeps_whole_files_where_recorded_coverage_cannot_speak(tmp_path, monkeypatch, cause):
    manifest = _recorded_project(tmp_path, monkeypatch)
    edits = {
        "helper": ("tests/conftest.py", "X = 1\n"),
        "dependency": ("uv.lock", "version = 1\n"),
        "root module": ("setup.py", "X = 1\n"),
        "workflow": (".github/workflows/test.yml", "env:\n  FLAG: 1\n"),
        "edited test": ("tests/test_core.py", "\n# edited\n"),
        "new function": ("scripts/core.py", "\n\ndef h(value):\n    return value * 2\n"),
        "unrecorded test": ("tests/test_core.py", "\n\ndef test_skipped_when_recorded():\n    pass\n"),
    }
    if cause in edits:
        path, text = edits[cause]
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        appended = cause in ("new function", "unrecorded test", "edited test")
        target.write_text((target.read_text() if appended else "") + text)
        head = _commit(tmp_path, cause)
        if cause == "unrecorded test":
            manifest["commit"] = head
    if cause == "no commit":
        del manifest["commit"]
    selection = {}
    plan.estimate(tmp_path, {"scripts/core.py": {10} if cause == "new function" else {6}}, selection, manifest)
    assert selection == {"scripts/core.py": WHOLE}


def test_a_changed_tests_data_file_adds_only_the_tests_that_read_it(tmp_path, monkeypatch):
    manifest = _recorded_project(tmp_path, monkeypatch)
    manifest["tests"]["tests/test_core.py::test_other"]["files"] = ["tests/data/rules.json"]
    (tmp_path / "tests/data").mkdir()
    (tmp_path / "tests/data/rules.json").write_text("{}\n")
    _commit(tmp_path, "data")
    selection = {}
    plan.estimate(tmp_path, {"scripts/core.py": {6}}, selection, manifest)
    assert selection == {"scripts/core.py": sorted([*NARROWED, "tests/test_core.py::test_other"])}


WORKFLOW = (
    "env:\n  FLAG: {env}\njobs:\n  split:\n    runs-on: {split}\n  unit:\n    needs: split\n"
    "    timeout-minutes: {timeout}\n    steps:\n      - run: {unit}\n  unit-selected:\n    steps:\n"
    "      - run: {selected}\n  lint:\n    steps:\n      - run: {lint}\n"
)
RECORDED_WORKFLOW = {"env": "1", "split": "x", "unit": "pytest", "timeout": 10, "selected": "pytest", "lint": "ruff"}


@pytest.mark.parametrize(
    "edit,expected",
    [
        ({"lint": "ruff check"}, sorted([*NARROWED, "tests/test_core.py::test_other"])),
        ({"split": "y"}, WHOLE),
        ({"timeout": 20}, sorted([*NARROWED, "tests/test_core.py::test_other"])),
        ({"unit": "pytest -x"}, WHOLE),
        ({"selected": "pytest -x"}, WHOLE),
        ({"env": "2"}, WHOLE),
    ],
)
def test_a_workflow_edit_drops_narrowing_only_when_it_changes_the_test_command(tmp_path, monkeypatch, edit, expected):
    manifest = {**_recorded_project(tmp_path, monkeypatch), "version": 2}
    manifest["tests"]["tests/test_core.py::test_other"]["files"] = [".github/workflows/test.yml"]
    workflow = tmp_path / ".github/workflows/test.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(WORKFLOW.format(**RECORDED_WORKFLOW))
    manifest["commit"] = _commit(tmp_path, "recorded workflow")
    workflow.write_text(WORKFLOW.format(**{**RECORDED_WORKFLOW, **edit}))
    _commit(tmp_path, "workflow")
    selection = {}
    plan.estimate(tmp_path, {"scripts/core.py": {6}}, selection, manifest)
    assert selection == {"scripts/core.py": expected}


def test_a_path_whose_covering_tests_are_gone_keeps_its_selected_files(tmp_path, monkeypatch):
    manifest = _recorded_project(tmp_path, monkeypatch)
    manifest["commit"] = subprocess.check_output([*GIT, "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    manifest["tests"] = {
        **{f"tests/test_core.py::{name}": {"units": [0], "escapes": False} for name in ("test_f", "test_g")},
        "tests/test_core.py::test_other": {"units": [2], "escapes": False},
        "tests/test_core.py::TestSpawn::test_child": {"units": [0], "escapes": False},
        "tests/test_edited.py::test_new": {"units": [0], "escapes": False},
        "tests/test_fresh.py::test_fresh": {"units": [2], "escapes": False},
        "tests/test_gone.py::test_g": {"units": [1], "escapes": False},
    }
    selection = {}
    plan.estimate(tmp_path, {"scripts/core.py": {6}}, selection, manifest)
    assert selection == {"scripts/core.py": WHOLE}


UNIT_JOBS = {"unit": {"needs": "split", "steps": [{"run": "pytest"}]}, "unit-selected": {"steps": [{"run": "pytest"}]}}


@pytest.mark.parametrize(
    "edit,expected",
    [
        ({"jobs": {**UNIT_JOBS, "split": {"runs-on": "y"}}}, True),
        ({"jobs": {**UNIT_JOBS, "unit-selected": {"needs": ["select"], "steps": [{"run": "pytest"}]}}}, False),
        ({"jobs": {**UNIT_JOBS, "lint": {"runs-on": "y"}}}, False),
        ({"jobs": {**UNIT_JOBS, "unit": {**UNIT_JOBS["unit"], "needs": ["split", "count"]}}}, False),
        ({"jobs": {**UNIT_JOBS, "unit": {**UNIT_JOBS["unit"], "if": "always()"}}}, False),
        ({"jobs": {**UNIT_JOBS, "unit": {**UNIT_JOBS["unit"], "outputs": {"a": "b"}}}}, False),
        ({"jobs": {**UNIT_JOBS, "unit": {**UNIT_JOBS["unit"], "runs-on": "other"}}}, True),
        ({"jobs": {**UNIT_JOBS, "unit-selected": {"steps": []}}}, True),
        ({"jobs": {**UNIT_JOBS, "mutation-stats": {"steps": [{"run": "collect"}]}}}, True),
        ({"jobs": {**UNIT_JOBS, "mutation-stats": {"needs": "mutation-plan"}}}, False),
        ({"jobs": {"unit": UNIT_JOBS["unit"]}}, True),
        ({"jobs": {**UNIT_JOBS, "unit": "pytest"}}, True),
        ({"jobs": {"unit-selected": UNIT_JOBS["unit-selected"]}}, True),
        ({"jobs": UNIT_JOBS, "defaults": {"run": {"shell": "sh"}}}, True),
        ({"jobs": UNIT_JOBS, "env": {"FLAG": "1"}}, True),
        ({"jobs": []}, True),
    ],
)
def test_only_the_jobs_running_recorded_tests_decide_a_collection_change(edit, expected):
    import yaml

    old = yaml.safe_dump({"jobs": UNIT_JOBS})
    jobs = plan.COLLECTING_JOBS[plan.TEST_WORKFLOW]
    assert plan.collection_changed(old, yaml.safe_dump(edit), jobs) is expected
    assert plan.collection_changed(old, old, jobs) is False
    assert plan.collection_changed(old, "jobs: [", jobs) is True
    unit_only = yaml.safe_dump({"jobs": {"unit": UNIT_JOBS["unit"]}})
    assert (
        plan.collection_changed(unit_only, yaml.safe_dump({"jobs": {"unit": UNIT_JOBS["unit"], "lint": {}}}), jobs)
        is False
    )
    preflight = plan.COLLECTING_JOBS[".github/workflows/mutation-preflight.yml"]
    stats = yaml.safe_dump({"jobs": {"stats": {"steps": [{"run": "collect"}]}, "plan": {}}})
    assert plan.collection_changed(stats, stats.replace("plan: {}", "plan: {a: 1}"), preflight) is False
    assert plan.collection_changed(stats, stats.replace("run: collect", "run: other"), preflight) is True


@pytest.mark.parametrize(
    "importer,extra,changed,narrowed",
    [
        ("from tests.helpers import H\n", {}, "tests/helpers.py", False),
        ("from tests import helpers\n", {}, "tests/helpers.py", False),
        ("from .helpers import H\n", {}, "tests/helpers.py", False),
        ("import helpers\n", {}, "tests/helpers.py", False),
        ('HELPER = "tests/helpers.py"\n', {}, "tests/helpers.py", False),
        ("from tests.chain import H\n", {"tests/chain.py": "from tests.helpers import H\n"}, "tests/helpers.py", False),
        ("", {"scripts/uses.py": "from tests.helpers import H\n"}, "tests/helpers.py", False),
        ("", {"tests/__init__.py": "X = 1\n"}, "tests/__init__.py", False),
        ("", {"tests/nested/conftest.py": "X = 1\n"}, "tests/nested/conftest.py", True),
        (
            "from tests.chain import H\n",
            {"tests/chain.py": "from tests.helpers import H\n", "tests/helpers.py": "from tests.chain import H\n"},
            "tests/helpers.py",
            False,
        ),
        ("import tests.pkg\n", {"tests/pkg/__init__.py": "X = 1\n"}, "tests/pkg/__init__.py", False),
        (
            "",
            {"tests/test_unrelated.py": "X = 1\n", "scripts/uses.py": "from tests.test_unrelated import X\n"},
            "tests/test_unrelated.py",
            False,
        ),
        ("", {"tests/test_unrelated.py": "X = 1\n"}, "tests/test_unrelated.py", True),
        ("from tests.test_shared import X\n", {"tests/test_shared.py": "X = 1\n"}, "tests/test_shared.py", False),
        ("", {}, "tests/helpers.py", True),
        ("from tests.helpers_extra import H\n", {"tests/helpers_extra.py": "H = 1\n"}, "tests/helpers.py", True),
        ("from tests.chain import H\n", {"tests/chain.py": "H = 1\n"}, "tests/helpers.py", True),
    ],
)
def test_a_shared_helper_edit_keeps_whole_only_the_test_files_it_reaches(
    tmp_path, monkeypatch, importer, extra, changed, narrowed
):
    manifest = _recorded_project(tmp_path, monkeypatch)
    core = tmp_path / "tests/test_core.py"
    core.write_text(importer + core.read_text())
    (tmp_path / "tests/helpers.py").write_text("H = 1\n")
    for path, text in extra.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    manifest["commit"] = _commit(tmp_path, "recorded helpers")
    (tmp_path / changed).write_text((tmp_path / changed).read_text() + "H = 2\n")
    _commit(tmp_path, "helper")
    selection = {}
    plan.estimate(tmp_path, {"scripts/core.py": {6}}, selection, manifest)
    assert selection == {"scripts/core.py": NARROWED if narrowed else WHOLE}


def test_an_unparsable_file_still_reaches_the_modules_its_text_names():
    assert plan._reach_keys("tests/a.py", "from tests.helpers import (\n") == {"tests", "tests.helpers"}
    assert plan._reach_keys("tests/pkg/__init__.py", "from . import b\n") == {"tests", "tests.pkg", "tests.pkg.b"}
    assert plan._reach_keys("tests/a.py", 'run("tests/x/y.py")\n') == {"tests/x/y.py", "y.py"}
    assert plan._reach_keys("tests/a.py", "# see tests/x/y.py.\n# not y.pyc\n") == {"tests/x/y.py", "y.py"}
    assert plan._reach_keys("tests/a/b.py", "from tests import c\n") == {"tests", "tests.c"}
    assert plan._reach_keys("tests/a/b.py", "from ..d import e\n") == {"tests", "tests.d", "tests.d.e"}


@pytest.mark.parametrize(
    "path,version,narrowed",
    [
        (".github/workflows/lint.yml", 2, True),
        (".github/workflows/lint.yml", 1, False),
        (".github/workflows/mutation-preflight.yml", 2, False),
        (".github/workflows/helper.py", 2, False),
    ],
)
def test_an_edit_to_a_workflow_that_runs_no_recorded_test_keeps_narrowing(
    tmp_path, monkeypatch, path, version, narrowed
):
    manifest = {**_recorded_project(tmp_path, monkeypatch), "version": version}
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_text("a: 1\n")
    _commit(tmp_path, "workflow")
    selection = {}
    plan.estimate(tmp_path, {"scripts/core.py": {6}}, selection, manifest)
    assert selection == {"scripts/core.py": NARROWED if narrowed else WHOLE}
