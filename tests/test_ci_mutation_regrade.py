import json
from pathlib import Path

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
    with pytest.raises(ValueError, match="do not cover exactly its graded files"):
        regrade(report, {}, CHANGES)


@pytest.mark.parametrize("changes", [{}, {**CHANGES, "hooks/other.py": {1}}])
def test_a_report_that_does_not_cover_the_changed_files_is_refused(changes: dict[str, set[int]]) -> None:
    with pytest.raises(ValueError, match="do not cover exactly the changed Python files"):
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


@pytest.mark.parametrize(
    ("claimed", "held"),
    [
        (["merge"], True),
        (["merge", "green"], True),
        (["merge", "green", "other"], False),
        (["green", "merge"], False),
        ([], False),
    ],
)
def test_planned_bases_hold_only_within_the_protected_choice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, claimed: list[str], held: bool
) -> None:
    calls = []

    def chosen(root: Path, base: str, head: str, graded: object) -> list[str]:
        calls.append((root, base, head, graded))
        return ["merge", "green"]

    monkeypatch.setattr(module, "own_bases", chosen)
    assert module.bases_held(tmp_path, "origin/dev", claimed) is held
    assert calls == [(tmp_path, "origin/dev", "HEAD", module.graded_green)]


@pytest.mark.parametrize(("bases", "status"), [("merge,green", 0), ("merge,other", 1)])
def test_main_holds_the_planned_bases(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, bases: str, status: int
) -> None:
    monkeypatch.setattr(module, "own_bases", lambda root, base, head, graded: ["merge", "green"])
    assert main(["bases", str(tmp_path), "origin/dev", bases]) == status
    expected = f"Protected grader: the planned mutation bases {bases} are not ones it would choose\n"
    assert capsys.readouterr().out == (expected if status else "")
