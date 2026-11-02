import json
from pathlib import Path

import pytest

from scripts.ci_mutation.clearances import write_clearance
from scripts.ci_mutation.regrade import main, regrade
from scripts.ci_mutation.runner import grade_shard_rows

PATH = "hooks/sample.py"
SURVIVOR = {"name": "hooks.sample.x_f__mutmut_1", "status": "survived", "lines": [2], "fingerprint": "abc", "diff": "d"}
KILLED = {"name": "hooks.sample.x_f__mutmut_2", "status": "killed", "lines": [2], "fingerprint": "def", "diff": "e"}
KEY = f"{PATH}:{SURVIVOR['name']}:abc"


def _lenient_report() -> dict:
    report = {"files": [], "not_mutated": [], "failed": False}
    graded = grade_shard_rows(report, PATH, [SURVIVOR, KILLED], {2}, {KEY: {"reader": "x", "reason": "y"}}, set())
    report["files"].append(graded)
    return report


def test_the_shard_records_every_graded_row_and_its_changed_lines() -> None:
    report = _lenient_report()
    assert report["graded"] == [{"path": PATH, "lines": [2], "rows": [SURVIVOR, KILLED]}]
    assert report["files"][0]["failures"] == []


def test_a_lenient_shard_grade_fails_under_the_protected_grader() -> None:
    graded = regrade(_lenient_report(), {})
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


def test_a_clearance_from_the_graded_tree_clears_the_survivor() -> None:
    ruling = {"reader": "Standards", "reason": "No observable effect"}
    graded = regrade(_lenient_report(), {KEY: ruling})
    assert graded["failed"] is False
    assert graded["files"][0]["cleared"] == [{**SURVIVOR, **ruling}]


def test_an_unmutated_file_fails_the_regrade() -> None:
    report = {**_lenient_report(), "not_mutated": [{"path": "hooks/other.py", "reason": "over budget"}]}
    assert regrade(report, {KEY: {"reader": "a", "reason": "b"}})["failed"] is True


@pytest.mark.parametrize("graded", [None, [], [{"path": "hooks/other.py", "lines": [2], "rows": []}]])
def test_rows_that_do_not_cover_the_graded_files_are_refused(graded: list[dict] | None) -> None:
    report = _lenient_report()
    if graded is None:
        del report["graded"]
    else:
        report["graded"] = graded
    with pytest.raises(ValueError, match="do not cover exactly"):
        regrade(report, {})


def test_an_empty_report_passes() -> None:
    assert regrade({"files": [], "not_mutated": [], "failed": True}, {})["failed"] is False


def test_main_grades_in_place_with_the_tree_clearances(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / ".mutation-gate" / "report.json"
    target.parent.mkdir()
    target.write_text(json.dumps(_lenient_report()))
    assert main([str(target), str(tmp_path)]) == 1
    assert json.loads(target.read_text())["failed"] is True
    out = capsys.readouterr().out
    assert out.startswith(f"survived on lines 2: {KEY}\n")
    assert out.endswith("Protected grader: mutation report failed\n")
    write_clearance(tmp_path, KEY, {"reader": "Standards", "reason": "No observable effect"})
    assert main([str(target), str(tmp_path)]) == 0
    assert json.loads(target.read_text())["files"][0]["failures"] == []
    assert capsys.readouterr().out == "Protected grader: mutation report passed\n"
