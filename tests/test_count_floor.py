import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[1]

SUITE = """
import pytest

def test_a():
    pass

@pytest.mark.parametrize("n", [1, 2])
def test_b(n):
    pass
"""
OTHER = """
def test_c():
    pass
"""


def _tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / "tests" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _floor(tmp_path, base: dict[str, str], head: dict[str, str], sources: dict[str, dict[str, str]] | None = None):
    for side, files in (sources or {}).items():
        for name, text in files.items():
            path = tmp_path / side / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.count_floor",
            "--base",
            str(_tree(tmp_path / "base", base)),
            "--head",
            str(_tree(tmp_path / "head", head)),
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )


def test_head_matching_the_base_count_passes(tmp_path):
    result = _floor(tmp_path, {"test_a.py": SUITE}, {"test_a.py": SUITE})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "head collects 3 tests, base floor 3" in result.stdout


def test_head_adding_tests_passes(tmp_path):
    result = _floor(tmp_path, {"test_a.py": SUITE}, {"test_a.py": SUITE, "test_c.py": OTHER})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "head collects 4 tests, base floor 3" in result.stdout


def test_a_deleted_test_file_is_red_and_names_every_lost_test(tmp_path):
    result = _floor(tmp_path, {"test_a.py": SUITE, "test_c.py": OTHER}, {"test_a.py": SUITE})
    assert result.returncode == 1
    assert "head collects 3 tests, base floor 4" in result.stdout
    assert "tests/test_c.py::test_c is in the base and not in the head" in result.stdout
    assert "::error::" in result.stdout


def test_a_renamed_test_keeps_the_floor(tmp_path):
    renamed = SUITE.replace("def test_a", "def test_renamed")
    result = _floor(tmp_path, {"test_a.py": SUITE}, {"test_a.py": renamed})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not in the head" not in result.stdout


@pytest.mark.parametrize("side", ["base", "head"])
def test_a_collection_error_on_either_side_is_red(tmp_path, side):
    broken = {"test_a.py": SUITE, "test_x.py": "import not_a_module_anywhere\n"}
    sides = {"base": {"test_a.py": SUITE}, "head": {"test_a.py": SUITE}}
    sides[side] = broken
    result = _floor(tmp_path, sides["base"], sides["head"])
    assert result.returncode == 1
    assert f"::error::Collecting the {side} tests failed" in result.stdout


SUBJECTS = "def subject():\n    return 1\n\n\ndef other():\n    return 2\n"
SUBJECT_TESTS = """
from pkg import mod

def test_subject():
    assert mod.subject() == 1

def test_other():
    assert mod.other() == 2
"""
OTHER_TEST = SUBJECT_TESTS.split("def test_subject")[0] + "def test_other" + SUBJECT_TESTS.split("def test_other")[1]


def _package(**modules: str) -> dict[str, str]:
    return {"pkg/__init__.py": "", **{f"pkg/{name}.py": text for name, text in modules.items()}}


def test_a_lost_test_whose_subject_survives_is_red(tmp_path):
    result = _floor(
        tmp_path,
        {"test_mod.py": SUBJECT_TESTS},
        {"test_mod.py": OTHER_TEST},
        {"base": _package(mod=SUBJECTS), "head": _package(mod=SUBJECTS)},
    )
    assert result.returncode == 1
    assert "tests/test_mod.py::test_subject is in the base and not in the head" in result.stdout


def test_a_lost_test_whose_subject_moved_to_another_module_is_red(tmp_path):
    subject, other = SUBJECTS.split("\n\n\n")
    result = _floor(
        tmp_path,
        {"test_mod.py": SUBJECT_TESTS},
        {"test_mod.py": OTHER_TEST},
        {"base": _package(mod=SUBJECTS), "head": _package(mod=other, elsewhere=subject)},
    )
    assert result.returncode == 1
    assert "tests/test_mod.py::test_subject is in the base and not in the head" in result.stdout


def test_a_lost_test_whose_subject_was_deleted_is_graded(tmp_path):
    _, kept = SUBJECTS.split("\n\n\n")
    result = _floor(
        tmp_path,
        {"test_mod.py": SUBJECT_TESTS},
        {"test_mod.py": OTHER_TEST},
        {"base": _package(mod=SUBJECTS), "head": _package(mod=kept)},
    )
    assert result.returncode in (0, 1), result.stdout + result.stderr
    assert "tests/test_mod.py::test_subject" in result.stdout


def test_a_deleted_subject_admits_only_the_tests_that_referenced_it(tmp_path):
    _, kept = SUBJECTS.split("\n\n\n")
    result = _floor(
        tmp_path,
        {"test_mod.py": SUBJECT_TESTS, "test_c.py": OTHER},
        {"test_c.py": OTHER},
        {"base": _package(mod=SUBJECTS), "head": _package(mod=kept)},
    )
    assert result.returncode == 1
    assert "tests/test_mod.py::test_other is in the base and not in the head" in result.stdout
