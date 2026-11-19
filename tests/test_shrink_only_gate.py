"""Tests for the shared load, write and CLI driver of a shrink only allowlist gate."""

from pathlib import Path

import pytest

from scripts import shrink_only_gate
from scripts.size_limits import GradeError

pytestmark = pytest.mark.unit

ALLOWLIST = Path("tests/EXAMPLE_ALLOWLIST.json")


def test_load_allowlist_returns_none_without_a_recorded_file(tmp_path: Path) -> None:
    assert shrink_only_gate.load_allowlist(tmp_path, ALLOWLIST) is None


def test_write_allowlist_then_load_allowlist_round_trips(tmp_path: Path) -> None:
    (tmp_path / ALLOWLIST.parent).mkdir(parents=True)
    shrink_only_gate.write_allowlist(tmp_path, ALLOWLIST, {"a.py": 2})
    assert shrink_only_gate.load_allowlist(tmp_path, ALLOWLIST) == {"a.py": 2}
    assert (tmp_path / ALLOWLIST).read_text() == '{\n "a.py": 2\n}\n'


def test_main_needs_a_base_without_write(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        shrink_only_gate.main([], ALLOWLIST, lambda tree: {}, lambda base, head, recorded: [], "noun")
    assert raised.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1].endswith(": error: grading needs --base")


def test_main_write_records_the_measurement(tmp_path: Path) -> None:
    (tmp_path / ALLOWLIST.parent).mkdir(parents=True)

    def measure(tree: Path) -> dict[str, int]:
        assert tree == tmp_path
        return {"a.py": 1}

    code = shrink_only_gate.main(
        ["--write", "--head", str(tmp_path)], ALLOWLIST, measure, lambda base, head, recorded: [], "noun"
    )
    assert code == 0
    assert shrink_only_gate.load_allowlist(tmp_path, ALLOWLIST) == {"a.py": 1}


def test_main_grades_the_head_against_the_base(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base, head = tmp_path / "base", tmp_path / "head"
    (base / ALLOWLIST.parent).mkdir(parents=True)
    (head / ALLOWLIST.parent).mkdir(parents=True)
    shrink_only_gate.write_allowlist(base, ALLOWLIST, {})
    shrink_only_gate.write_allowlist(head, ALLOWLIST, {"a.py": 1})

    def measure(tree: Path) -> dict[str, int]:
        assert tree == head
        return {"a.py": 1}

    def grade(base: dict[str, int] | None, head: dict[str, int], recorded: dict[str, int] | None) -> list[str]:
        allowed = base or {}
        return [f"{name} over" for name, count in head.items() if count > allowed.get(name, 0)]

    code = shrink_only_gate.main(["--base", str(base), "--head", str(head)], ALLOWLIST, measure, grade, "noun")
    assert code == 1
    assert capsys.readouterr().out == "1 files noun, 0 on the base allowlist, 1 errors\n::error::a.py over\n"


def test_main_prints_a_grade_error_and_exits_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def measure(tree: Path) -> dict[str, int]:
        raise GradeError("boom")

    code = shrink_only_gate.main(["--base", str(tmp_path)], ALLOWLIST, measure, lambda base, head, recorded: [], "noun")
    assert code == 1
    assert capsys.readouterr().out == "::error::boom\n"
