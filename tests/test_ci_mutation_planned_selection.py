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
    if mode in {"legacy", "inserted", "header", "drift", "module"}:
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
