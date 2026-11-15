import json
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_mutation import regrade as module
from scripts.ci_mutation.clearances import write_clearance
from scripts.ci_mutation.regrade import main, regrade
from scripts.ci_mutation.runner import grade_shard_rows

PATH = "hooks/sample.py"
SURVIVOR = {"name": "hooks.sample.x_f__mutmut_1", "status": "survived", "lines": [2], "fingerprint": "abc", "diff": "d"}
KILLED = {"name": "hooks.sample.x_f__mutmut_2", "status": "killed", "lines": [2], "fingerprint": "def", "diff": "e"}
KEY = f"{PATH}:{SURVIVOR['name']}:abc"
CHANGES = {PATH: {2}}


def _lenient_report(lines: set[int]) -> dict:
    report = {"files": [], "not_mutated": [], "failed": False}
    graded = grade_shard_rows(report, PATH, [SURVIVOR, KILLED], lines, {KEY: {"reader": "x", "reason": "y"}}, set())
    report["files"].append(graded)
    return report


def test_the_shard_records_every_graded_row_and_its_changed_lines() -> None:
    report = _lenient_report({2})
    assert report["graded"] == [{"path": PATH, "lines": [2], "rows": [SURVIVOR, KILLED]}]
    assert report["files"][0]["failures"] == []


def test_a_lenient_shard_grade_fails_under_the_protected_grader() -> None:
    graded = regrade(_lenient_report({2}), {}, CHANGES)
    assert graded["failed"] is True
    assert graded["files"] == [
        {
            "path": PATH,
            "counts": {"survived": 1, "killed": 1},
            "failures": [SURVIVOR],
            "untouched_survivors": [],
            "cleared": [],
        }
    ]


def test_the_protected_changed_lines_replace_the_recorded_ones() -> None:
    report = _lenient_report(set())
    assert regrade(report, {}, {PATH: set()})["files"][0]["untouched_survivors"] == [SURVIVOR]
    assert regrade(report, {}, CHANGES)["files"][0]["failures"] == [SURVIVOR]


def test_a_clearance_from_the_graded_tree_clears_the_survivor() -> None:
    ruling = {"reader": "Standards", "reason": "No observable effect"}
    graded = regrade(_lenient_report({2}), {KEY: ruling}, CHANGES)
    assert graded["failed"] is False
    assert graded["files"][0]["cleared"] == [{**SURVIVOR, **ruling}]


def test_an_unmutated_file_counts_as_covered_and_fails_the_regrade() -> None:
    report = {**_lenient_report({2}), "not_mutated": [{"path": "hooks/other.py", "reason": "over budget"}]}
    changes = {**CHANGES, "hooks/other.py": {1}}
    assert regrade(report, {KEY: {"reader": "a", "reason": "b"}}, changes)["failed"] is True


@pytest.mark.parametrize("graded", [None, [], [{"path": "hooks/other.py", "lines": [2], "rows": []}]])
def test_rows_that_do_not_cover_the_graded_files_are_refused(graded: list[dict] | None) -> None:
    report = _lenient_report({2})
    if graded is None:
        del report["graded"]
    else:
        report["graded"] = graded
    with pytest.raises(ValueError, match=r"^Mutation report rows do not cover exactly its graded files$"):
        regrade(report, {}, CHANGES)


@pytest.mark.parametrize("changes", [{}, {**CHANGES, "hooks/other.py": {1}}])
def test_a_report_that_does_not_cover_the_changed_files_is_refused(changes: dict[str, set[int]]) -> None:
    with pytest.raises(ValueError, match=r"^Mutation report does not cover exactly the changed Python files$"):
        regrade(_lenient_report({2}), {}, changes)


def test_an_empty_report_passes_only_without_changes() -> None:
    empty = {"files": [], "not_mutated": [], "failed": True}
    assert regrade(empty, {}, {})["failed"] is False
    with pytest.raises(ValueError, match="changed Python files"):
        regrade(empty, {}, CHANGES)


def test_main_grades_in_place_with_the_tree_clearances_and_protected_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def changes(root: Path, bases: list[str], head: str) -> dict[str, set[int]]:
        calls.append((root, bases, head))
        return CHANGES

    monkeypatch.setattr(module, "discover_changes", changes)
    monkeypatch.setattr(module, "recollect", lambda output, wanted, shard: {PATH: [SURVIVOR, KILLED]})
    target = tmp_path / ".mutation-gate" / "report.json"
    target.parent.mkdir()
    target.write_text(json.dumps(_lenient_report({2})))
    assert main(["report", str(target), str(tmp_path), "one,two"]) == 1
    assert calls == [(tmp_path, ["one", "two"], "HEAD")]
    assert json.loads(target.read_text())["failed"] is True
    out = capsys.readouterr().out
    assert out.startswith(f"survived on lines 2: {KEY}\n")
    assert out.endswith("Protected grader: mutation report failed\n")
    write_clearance(tmp_path, KEY, {"reader": "Standards", "reason": "No observable effect"})
    assert main(["report", str(target), str(tmp_path), "one"]) == 0
    assert json.loads(target.read_text())["files"][0]["failures"] == []
    assert capsys.readouterr().out == "Protected grader: mutation report passed\n"
    target.write_text(json.dumps({"files": [], "not_mutated": [], "failed": False}))
    with pytest.raises(ValueError, match=r"^Mutation report does not cover exactly the changed Python files$"):
        main(["report", str(target), str(tmp_path), "one"])


def test_bases_prints_the_protected_choice_for_the_step_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls = []

    def chosen(root: Path, base: str, head: str, graded: object) -> list[str]:
        calls.append((root, base, head, graded))
        return ["merge", "green"]

    monkeypatch.setattr(module, "own_bases", chosen)
    assert main(["bases", str(tmp_path), "origin/dev"]) == 0
    assert calls == [(tmp_path, "origin/dev", "HEAD", module.graded_green)]
    assert capsys.readouterr().out == "bases=merge,green\n"


FORGED = {**SURVIVOR, "status": "killed"}
UNIT = {**SURVIVOR, "name": "hooks.sample.x_f__mutmut_3"}


def test_rebuild_replaces_forged_runner_statuses_with_the_protected_rows(tmp_path: Path) -> None:
    report = {"files": [], "not_mutated": [], "graded": [{"path": PATH, "lines": [], "rows": [FORGED]}]}
    report["graded"].append({"path": "hooks/other.py", "lines": [], "rows": []})
    rebuilt = module.rebuild(report, {PATH: [SURVIVOR, KILLED]}, set(), CHANGES, tmp_path)
    other = {"path": "hooks/other.py", "lines": [], "rows": []}
    assert rebuilt["graded"] == [{"path": PATH, "lines": [2], "rows": [SURVIVOR, KILLED]}, other]
    assert rebuilt["units"] == []
    assert module.rebuild({"files": []}, {}, set(), {}, tmp_path) == {"files": [], "graded": [], "units": []}


def test_rebuild_moves_protected_split_names_into_units(tmp_path: Path) -> None:
    report = {"files": [], "not_mutated": [], "graded": [{"path": PATH, "lines": [2], "rows": []}], "units": []}
    rebuilt = module.rebuild(report, {PATH: [SURVIVOR, UNIT]}, {UNIT["name"]}, CHANGES, tmp_path)
    assert rebuilt["graded"] == [{"path": PATH, "lines": [2], "rows": [SURVIVOR]}]
    assert rebuilt["units"] == [{"path": PATH, "lines": [2], "rows": [UNIT]}]


def test_rebuild_refuses_a_graded_file_with_functions_the_collector_did_not_read(tmp_path: Path) -> None:
    (tmp_path / "hooks").mkdir()
    (tmp_path / PATH).write_text("def f():\n    return 1\n")
    report = {"files": [], "not_mutated": [], "graded": [{"path": PATH, "lines": [2], "rows": []}]}
    with pytest.raises(ValueError, match="Protected collector read no mutation rows for hooks/sample.py"):
        module.rebuild(report, {}, set(), CHANGES, tmp_path)
    (tmp_path / PATH).write_text("VALUE = 1\n")
    assert module.rebuild(report, {}, set(), CHANGES, tmp_path)["graded"][0]["rows"] == []


def test_recollect_runs_this_copys_collector_in_each_work_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folders = {"0-a": ["tests/test_x.py"], "1-b": [PATH, "hooks/other.py"], "2-c": [PATH]}
    for name, paths in folders.items():
        (tmp_path / name).mkdir()
        (tmp_path / name / "changed-lines.json").write_text(json.dumps(dict.fromkeys(paths, {})))
    calls = []
    found = {"1-b": [SURVIVOR], "2-c": [KILLED]}

    def run(command: list[str], cwd: Path, env: dict[str, str], check: bool) -> None:
        calls.append((command, cwd, env["PYTHONPATH"], env["PYTHONSAFEPATH"], check))
        Path(command[3]).write_text(json.dumps({PATH: found[cwd.name]}))

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.chdir(tmp_path)
    assert module.recollect(Path("."), {PATH}, (1, 3)) == {PATH: [SURVIVOR, KILLED]}
    expected = []
    for name in ("1-b", "2-c"):
        folder = tmp_path.resolve() / name
        command = [module.sys.executable, "-m", "scripts.ci_mutation.report", str(folder / "protected-results.json")]
        expected.append(([*command, "1", "3", PATH], folder, str(module.GRADER), "1", True))
    assert calls == expected
    assert module.GRADER == Path(module.__file__).resolve().parents[2]


def _shard(folder: Path, shard: int, rows: list[dict[str, Any]]) -> None:
    target = folder / f"mutation-report-{shard}" / "report.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps({"files": [], "not_mutated": [], "units": [{"path": PATH, "lines": [], "rows": rows}]})
    )


@pytest.mark.parametrize(("second", "status"), [({**UNIT, "status": "killed"}, 0), (UNIT, 1)])
def test_units_grade_every_shards_slices_together_on_the_protected_lines(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    second: dict[str, Any],
    status: int,
) -> None:
    calls = []

    def changes(root: Path, bases: list[str], head: str) -> dict[str, set[int]]:
        calls.append((root, bases, head))
        return CHANGES

    monkeypatch.setattr(module, "discover_changes", changes)
    _shard(tmp_path / "reports", 0, [UNIT])
    _shard(tmp_path / "reports", 1, [second])
    other = {"path": "hooks/other.py", "lines": [1], "rows": [{**KILLED, "name": "hooks.other.x_g__mutmut_1"}]}
    third = tmp_path / "reports" / "mutation-report-2" / "report.json"
    third.parent.mkdir()
    third.write_text(json.dumps({"files": [], "not_mutated": []}))
    fourth = tmp_path / "reports" / "mutation-report-3" / "report.json"
    fourth.parent.mkdir()
    fourth.write_text(json.dumps({"files": [], "not_mutated": [], "units": [other]}))
    assert main(["units", str(tmp_path / "reports"), "4", str(tmp_path), "merge,green"]) == status
    assert calls == [(tmp_path, ["merge", "green"], "HEAD")]
    verdict = "failed" if status else "passed"
    out = capsys.readouterr().out
    assert out.endswith(f"Protected grader: split mutation units {verdict}\n")
    assert out.startswith("survived on lines 2: ") is bool(status)


def test_units_fail_when_a_shard_report_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(module, "discover_changes", lambda root, bases, head: CHANGES)
    _shard(tmp_path, 0, [{**UNIT, "status": "killed"}])
    assert module.grade_units(tmp_path, 3, tmp_path, ["merge"]) == 1
    missing = [(tmp_path / f"mutation-report-{shard}" / "report.json").as_posix() for shard in (1, 2)]
    assert capsys.readouterr().out == f"Protected grader: missing mutation shard reports {missing[0]}, {missing[1]}\n"


def test_a_single_shard_report_grades_its_split_units_in_place(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(module, "discover_changes", lambda root, bases, head: CHANGES)
    calls = []

    def collected(output: Path, wanted: set[str], shard: tuple[int, int]) -> dict[str, list[dict]]:
        calls.append((output, wanted, shard))
        return {PATH: [KILLED, UNIT]}

    monkeypatch.setattr(module, "recollect", collected)
    stats = tmp_path / ".mutation-stats"
    stats.mkdir()
    (stats / "plan.json").write_text(json.dumps({"assignments": {UNIT["name"]: [0]}}))
    target = tmp_path / ".mutation-gate" / "report.json"
    target.parent.mkdir()
    target.write_text(json.dumps(_lenient_report({2})))
    assert main(["report", str(target), str(tmp_path), "merge", "--stats", str(stats)]) == 1
    written = json.loads(target.read_text())
    assert written["units"] == [{"path": PATH, "lines": [2], "rows": [UNIT]}]
    assert written["failed"] is True
    command = ["report", str(target), str(tmp_path), "merge", "--shard", "1", "--shards", "2", "--stats", str(stats)]
    assert main(command) == 0
    assert calls == [(target.parent, {PATH}, (0, 1)), (target.parent, {PATH}, (1, 2))]
    (tmp_path / "hooks").mkdir()
    (tmp_path / PATH).write_text("def f():\n    return 1\n")
    monkeypatch.setattr(module, "recollect", lambda output, wanted, shard: {})
    with pytest.raises(ValueError, match="^Protected collector read no mutation rows for hooks/sample.py$"):
        main(["report", str(target), str(tmp_path), "merge"])
