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


SUBJECT = "def subject():\n    return 1\n"
KEPT = "def other():\n    return 2\n"
SUBJECT_TESTS = """
from pkg import mod

def test_subject():
    assert mod.subject() == 1

def test_other():
    assert mod.other() == 2
"""
OTHER_TEST = """
from pkg import mod

def test_other():
    assert mod.other() == 2
"""


def _package(**modules: str) -> dict[str, str]:
    return {"pkg/__init__.py": "", **{f"pkg/{name}.py": text for name, text in modules.items()}}


def _lose_test_subject(tmp_path, head_sources: dict[str, str], head_tests: dict[str, str] | None = None):
    return _floor(
        tmp_path,
        {"test_mod.py": SUBJECT_TESTS},
        head_tests or {"test_mod.py": OTHER_TEST},
        {"base": _package(mod=SUBJECT + "\n\n" + KEPT), "head": head_sources},
    )


def test_a_lost_test_whose_subject_survives_is_red(tmp_path):
    result = _lose_test_subject(tmp_path, _package(mod=SUBJECT + "\n\n" + KEPT))
    assert result.returncode == 1
    assert "tests/test_mod.py::test_subject is in the base and not in the head" in result.stdout


def test_a_lost_test_whose_subject_moved_to_another_module_is_red(tmp_path):
    result = _lose_test_subject(tmp_path, _package(mod=KEPT, elsewhere=SUBJECT))
    assert result.returncode == 1
    assert "tests/test_mod.py::test_subject is in the base and not in the head" in result.stdout


def test_a_lost_test_whose_subject_was_deleted_is_admitted(tmp_path):
    result = _lose_test_subject(tmp_path, _package(mod=KEPT))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/test_mod.py::test_subject is admitted: the head deletes pkg.mod.subject, which it referenced" in (
        result.stdout
    )
    assert "head collects 1 tests, base floor 1" in result.stdout


@pytest.mark.parametrize(
    "survivor",
    [
        "class Runner:\n    def subject(self):\n        return 1\n",
        "from pkg.mod import other as subject\n",
        "subject = 1\n",
    ],
    ids=["method", "import", "assignment"],
)
def test_a_subject_name_still_bound_in_the_head_admits_nothing(tmp_path, survivor):
    result = _lose_test_subject(tmp_path, _package(mod=KEPT, elsewhere=survivor))
    assert result.returncode == 1
    assert "tests/test_mod.py::test_subject is in the base and not in the head" in result.stdout


def test_a_subject_moved_into_the_tests_tree_admits_nothing(tmp_path):
    head_tests = {"test_mod.py": OTHER_TEST, "floor_helpers.py": SUBJECT}
    result = _lose_test_subject(tmp_path, _package(mod=KEPT), head_tests)
    assert result.returncode == 1
    assert "tests/test_mod.py::test_subject is in the base and not in the head" in result.stdout


@pytest.mark.parametrize(
    "sources",
    [
        {**_package(mod=KEPT), "broken.py": "def (:\n"},
        _package(mod=KEPT + "\nfrom pkg.elsewhere import *\n", elsewhere=SUBJECT.replace("subject", "renamed")),
    ],
    ids=["unparseable", "star-import"],
)
def test_a_head_file_that_hides_its_names_admits_nothing(tmp_path, sources):
    result = _lose_test_subject(tmp_path, sources)
    assert result.returncode == 1
    assert "hides its names in the head (unparseable or a star import), so no lost test is admitted" in result.stdout
    assert "tests/test_mod.py::test_subject is in the base and not in the head" in result.stdout


@pytest.mark.parametrize(
    "survivor",
    [
        "try:\n    pass\nexcept ValueError as subject:\n    pass\n",
        "match 1:\n    case subject:\n        pass\n",
    ],
    ids=["except", "match"],
)
def test_a_subject_name_rebound_by_except_or_match_admits_nothing(tmp_path, survivor):
    result = _lose_test_subject(tmp_path, _package(mod=KEPT, elsewhere=survivor))
    assert result.returncode == 1
    assert "tests/test_mod.py::test_subject is in the base and not in the head" in result.stdout


def test_an_attribute_of_a_non_module_never_matches_a_deleted_definition(tmp_path):
    base = "def test_lookup():\n    assert {1: 2}.get(1)\n"
    result = _floor(
        tmp_path,
        {"test_mod.py": base, "test_c.py": OTHER},
        {"test_c.py": OTHER},
        {"base": _package(mod="def get():\n    return 1\n"), "head": _package(mod="")},
    )
    assert result.returncode == 1
    assert "tests/test_mod.py::test_lookup is in the base and not in the head" in result.stdout


def test_a_lost_class_test_whose_imported_subject_was_deleted_is_admitted(tmp_path):
    base = "from pkg.mod import Subject\n\n\nclass TestSubject:\n    def test_it(self):\n        assert Subject\n"
    result = _floor(
        tmp_path,
        {"test_mod.py": base, "test_c.py": OTHER},
        {"test_c.py": OTHER},
        {"base": _package(mod="class Subject:\n    pass\n"), "head": _package(mod="")},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/test_mod.py::TestSubject::test_it is admitted: the head deletes pkg.mod.Subject" in result.stdout


def test_a_subject_deleted_only_from_the_tests_tree_admits_nothing(tmp_path):
    base = "from floor_helpers import subject\n\n\ndef test_subject():\n    assert subject()\n"
    result = _floor(
        tmp_path,
        {"test_mod.py": base, "floor_helpers.py": SUBJECT, "test_c.py": OTHER},
        {"test_c.py": OTHER},
    )
    assert result.returncode == 1
    assert "tests/test_mod.py::test_subject is in the base and not in the head" in result.stdout


def test_a_deleted_subject_admits_only_the_tests_that_referenced_it(tmp_path):
    result = _floor(
        tmp_path,
        {"test_mod.py": SUBJECT_TESTS, "test_c.py": OTHER},
        {"test_c.py": OTHER},
        {"base": _package(mod=SUBJECT + "\n\n" + KEPT), "head": _package(mod=KEPT)},
    )
    assert result.returncode == 1
    assert "tests/test_mod.py::test_subject is admitted" in result.stdout
    assert "head collects 1 tests, base floor 2" in result.stdout
    assert "tests/test_mod.py::test_other is in the base and not in the head" in result.stdout


@pytest.mark.parametrize("constant", ['FORM = "queue"\n', 'FORM: str = "queue"\n'], ids=["assign", "annotated"])
@pytest.mark.parametrize(
    "test",
    [
        "from pkg.mod import FORM\n\n\ndef test_form():\n    assert FORM\n",
        "from pkg import mod\n\n\ndef test_form():\n    assert mod.FORM\n",
    ],
    ids=["imported", "attribute"],
)
def test_a_lost_test_whose_constant_the_head_deletes_is_admitted(tmp_path, constant, test):
    result = _floor(
        tmp_path,
        {"test_mod.py": test, "test_c.py": OTHER},
        {"test_c.py": OTHER},
        {"base": _package(mod=constant + KEPT), "head": _package(mod=KEPT)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "tests/test_mod.py::test_form is admitted: the head deletes pkg.mod.FORM, which it referenced" in result.stdout
    )


def test_a_lost_test_whose_constant_the_head_keeps_is_red(tmp_path):
    test = "from pkg import mod\n\n\ndef test_form():\n    assert mod.FORM\n"
    source = _package(mod='FORM = "queue"\n' + KEPT)
    result = _floor(
        tmp_path, {"test_mod.py": test, "test_c.py": OTHER}, {"test_c.py": OTHER}, {"base": source, "head": source}
    )
    assert result.returncode == 1
    assert "tests/test_mod.py::test_form is in the base and not in the head" in result.stdout


BRANCHED = """
def steps(queue=False, selected=False):
    run = "plain"
    if queue:
        run += " queue"
    return run + " selected" if selected else run
"""
RETAINED = 'def steps(selected=False):\n    return "plain selected" if selected else "plain"\n'
BRANCH_TESTS = """
from pkg import mod

def test_queue():
    assert mod.steps(queue=True) == "plain queue"

def test_default():
    assert mod.steps() == "plain"

def test_selected():
    assert mod.steps(selected=True) == "plain selected"
"""
SELECTED_TEST = """
from pkg import mod

def test_selected():
    assert mod.steps(selected=True) == "plain selected"
"""


def _lose_branch_tests(tmp_path, base_source: str, head_sources: dict[str, str]):
    return _floor(
        tmp_path,
        {"test_mod.py": BRANCH_TESTS},
        {"test_mod.py": SELECTED_TEST},
        {"base": _package(mod=base_source), "head": head_sources},
    )


@pytest.mark.parametrize(
    "base",
    [
        BRANCHED,
        'def steps(queue=False, selected=False):\n    return "plain queue" if queue else "plain"\n',
        'def steps(queue=False, selected=False):\n    while queue:\n        return "plain queue"\n    return "plain"\n',
    ],
    ids=["if", "conditional", "while"],
)
def test_a_lost_test_whose_keyword_branch_the_head_deletes_is_admitted(tmp_path, base):
    result = _lose_branch_tests(tmp_path, base, _package(mod=RETAINED))
    assert result.returncode == 1
    assert "tests/test_mod.py::test_queue is admitted: the head deletes pkg.mod.steps(queue), which it referenced" in (
        result.stdout
    )
    assert "head collects 1 tests, base floor 2" in result.stdout
    assert "tests/test_mod.py::test_default is in the base and not in the head" in result.stdout


@pytest.mark.parametrize(
    "head",
    [
        _package(mod=BRANCHED),
        _package(mod='def steps(selected=False, **options):\n    return "plain"\n'),
        _package(mod=KEPT, elsewhere=RETAINED),
        _package(mod=BRANCHED.replace("queue=False, ", "queue=False, /, ")),
        _package(mod=BRANCHED.replace("queue", "lane")),
        _package(mod=BRANCHED.replace("queue=False, selected=False", "*queue, selected=False")),
        _package(mod="if True:\n" + RETAINED.replace("\n    ", "\n        ").replace("def", "    def")),
    ],
    ids=["kept", "kwargs", "moved", "positional", "renamed", "variadic", "nested"],
)
def test_a_keyword_the_head_still_accepts_moved_or_renamed_admits_nothing(tmp_path, head):
    result = _lose_branch_tests(tmp_path, BRANCHED, head)
    assert result.returncode == 1
    assert "tests/test_mod.py::test_queue is in the base and not in the head" in result.stdout


@pytest.mark.parametrize(
    "base",
    [
        'def steps(queue=False, selected=False):\n    return f"plain {queue} {selected}"\n',
        BRANCHED.replace('run = "plain"', 'run = "plain"\n    queue = queue or selected'),
    ],
    ids=["unbranched", "rebound"],
)
def test_a_removed_parameter_that_selected_no_branch_admits_nothing(tmp_path, base):
    result = _lose_branch_tests(tmp_path, base, _package(mod=RETAINED))
    assert result.returncode == 1
    assert "tests/test_mod.py::test_queue is in the base and not in the head" in result.stdout


def test_a_head_file_that_hides_its_names_admits_no_deleted_branch(tmp_path):
    result = _lose_branch_tests(tmp_path, BRANCHED, {**_package(mod=RETAINED), "broken.py": "def (:\n"})
    assert result.returncode == 1
    assert "hides its names in the head (unparseable or a star import), so no lost test is admitted" in result.stdout
    assert "tests/test_mod.py::test_queue is in the base and not in the head" in result.stdout
