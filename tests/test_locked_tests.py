import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import locked_tests

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit

LOCKED = locked_tests.LOCKED[0]
GUARD = """import pytest

CASES = [("a", 1), ("b", 2)]


def _blocked(command):
    assert command


@pytest.mark.parametrize("command,code", CASES)
def test_blocks(command, code):
    _blocked(command)
    assert code


class TestGuard:
    def test_raises(self):
        with pytest.raises(ValueError):
            int("x")

    def test_no_raise(self):
        str(1)
"""


def _tree(root: Path, text: str, record: str | None = None) -> Path:
    (root / LOCKED).parent.mkdir(parents=True, exist_ok=True)
    (root / LOCKED).write_text(record if record is not None else text)
    assert locked_tests.main(["--write", "--head", str(root)]) == 0
    (root / LOCKED).write_text(text)
    return root


def _grade(base: Path, head: Path, capsys) -> tuple[int, str]:
    code = locked_tests.main(["--base", str(base), "--head", str(head)])
    return code, capsys.readouterr().out


def test_the_unchanged_guard_tests_are_green(tmp_path, capsys):
    base = _tree(tmp_path / "base", GUARD)
    code, out = _grade(base, _tree(tmp_path / "head", GUARD), capsys)
    assert code == 0
    assert "4 locked tests on the base, 4 measured on the head, 0 errors" in out


def test_the_manifest_counts_assertions_calls_and_cases(tmp_path):
    tree = _tree(tmp_path, GUARD)
    assert locked_tests.load(tree) == {
        f"{LOCKED}::_blocked": {"assertions": 1, "calls": 0, "cases": 1},
        f"{LOCKED}::test_blocks": {"assertions": 2, "calls": 1, "cases": 2},
        f"{LOCKED}::TestGuard::test_raises": {"assertions": 1, "calls": 2, "cases": 1},
        f"{LOCKED}::TestGuard::test_no_raise": {"assertions": 0, "calls": 1, "cases": 1},
    }


@pytest.mark.parametrize(
    "planted",
    [
        GUARD.replace("@pytest.mark.parametrize", "@pytest.mark.skip\n@pytest.mark.parametrize"),
        GUARD.replace("    def test_raises", "    @pytest.mark.xfail\n    def test_raises"),
        GUARD.replace("class TestGuard:\n", "class TestGuard:\n    pytestmark = pytest.mark.skip\n"),
        GUARD.replace("import pytest\n", "import pytest\n\npytestmark = [pytest.mark.skipif(True, reason='x')]\n"),
        GUARD.replace("    _blocked(command)\n", "    pytest.skip('later')\n    _blocked(command)\n"),
        GUARD.replace('CASES = [("a", 1), ("b", 2)]', 'CASES = [pytest.param("a", 1, marks=pytest.mark.skip)]'),
        GUARD.replace("import pytest\n", "import pytest\nfrom pytest import xfail\n").replace(
            "        str(1)\n", "        xfail()\n        str(1)\n"
        ),
        GUARD.replace("import pytest\n", "import pytest as pt\n").replace("        str(1)\n", "        pt.skip()\n"),
        GUARD.replace("def _blocked(command):\n", "def _gate():\n    pytest.skip('x')\n\n\ndef _blocked(command):\n")
        .replace("        str(1)\n", "        _later()\n        str(1)\n")
        .replace("def _gate():", "def _later():\n    _gate()\n\n\ndef _gate():"),
        GUARD.replace("        str(1)\n", "        return\n        str(1)\n"),
        GUARD.replace("        str(1)\n", "        self._gate()\n        str(1)\n").replace(
            "    def test_raises", "    def _gate(self):\n        pytest.skip('x')\n\n    def test_raises"
        ),
        GUARD.replace(
            "        str(1)\n", "        request.applymarker(pytest.mark.skip(reason='x'))\n        str(1)\n"
        ),
        GUARD.replace("        str(1)\n", "        pytest.exit('x', returncode=0)\n        str(1)\n"),
    ],
    ids=[
        "skip",
        "xfail",
        "class mark",
        "module mark",
        "skip call",
        "skipped case",
        "imported skip",
        "aliased pytest",
        "skipping helper",
        "early return",
        "skipping method",
        "applied marker",
        "exit",
    ],
)
def test_a_skipped_or_xfailed_locked_test_is_red(tmp_path, capsys, planted):
    base = _tree(tmp_path / "base", GUARD)
    head = _tree(tmp_path / "head", planted, record=GUARD)
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "is skipped, xfailed or returns early." in out


def test_a_deleted_locked_test_is_red(tmp_path, capsys):
    base = _tree(tmp_path / "base", GUARD)
    head = _tree(tmp_path / "head", GUARD.replace("test_no_raise", "check_no_raise"))
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert f"::error::The locked test {LOCKED}::TestGuard::test_no_raise is deleted or renamed.\n" in out
    assert f"The head {locked_tests.MANIFEST} drops or lowers the locked assertions of" in out


@pytest.mark.parametrize(
    "planted,message",
    [
        (GUARD.replace("    assert code\n", ""), "test_blocks has 1 assertions, the base locks 2."),
        (GUARD.replace("    assert command\n", "    pass\n"), "_blocked is deleted or renamed."),
        (GUARD.replace(', ("b", 2)', ""), "test_blocks has 1 cases, the base locks 2."),
        (GUARD.replace("        str(1)\n", "        pass\n"), "test_no_raise has 0 calls, the base locks 1."),
    ],
    ids=["assert", "helper", "case", "call"],
)
def test_a_weakened_locked_test_is_red_even_when_rerecorded(tmp_path, capsys, planted, message):
    base = _tree(tmp_path / "base", GUARD)
    code, out = _grade(base, _tree(tmp_path / "head", planted), capsys)
    assert code == 1
    assert message in out


def test_a_manifest_recording_more_than_the_head_measures_is_red(tmp_path, capsys):
    base = _tree(tmp_path / "base", GUARD)
    head = _tree(tmp_path / "head", GUARD.replace("    def test_no_raise", "    def test_more"), record=GUARD)
    data = locked_tests.load(head)
    data[f"{LOCKED}::TestGuard::test_more"] = {"assertions": 5, "calls": 0, "cases": 1}
    (head / locked_tests.MANIFEST).write_text(json.dumps(data))
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert f"records more than {LOCKED}::TestGuard::test_more measures" in out


def test_a_new_skipped_test_in_a_locked_file_is_red(tmp_path, capsys):
    base = _tree(tmp_path / "base", GUARD)
    head = _tree(tmp_path / "head", GUARD + "\n\n@pytest.mark.skip\ndef test_new():\n    assert True\n")
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert f"::error::The locked test {LOCKED}::test_new is skipped, xfailed or returns early.\n" in out


def test_only_assert_methods_and_asserting_helpers_count_as_assertions(tmp_path):
    text = GUARD.replace("        str(1)\n", "        assertion_noop()\n        mock.assert_called_once()\n")
    _tree(tmp_path, text)
    assert locked_tests.load(tmp_path)[f"{LOCKED}::TestGuard::test_no_raise"]["assertions"] == 1


@pytest.mark.parametrize(
    "config", ["tests/conftest.py", "tests/context/conftest.py", "pyproject.toml", ".github/workflows/test.yml"]
)
def test_a_config_naming_a_locked_module_is_red(tmp_path, capsys, config):
    base = _tree(tmp_path / "base", GUARD)
    head = _tree(tmp_path / "head", GUARD)
    (head / config).parent.mkdir(parents=True, exist_ok=True)
    (head / config).write_text(f'collect_ignore = ["{Path(LOCKED).name}"]\n')
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert f"::error::{config} names the locked module {Path(LOCKED).stem};" in out


def test_a_base_without_a_manifest_locks_the_head(tmp_path, capsys):
    base = tmp_path / "base"
    base.mkdir()
    code, out = _grade(base, _tree(tmp_path / "head", GUARD), capsys)
    assert code == 0
    assert "::notice::The base has no" in out


def test_a_head_without_a_manifest_is_red(tmp_path, capsys):
    base = _tree(tmp_path / "base", GUARD)
    head = _tree(tmp_path / "head", GUARD)
    (head / locked_tests.MANIFEST).unlink()
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "::error::the head has no" in out


@pytest.mark.parametrize(
    "path,text",
    [
        (locked_tests.MANIFEST, "{"),
        (locked_tests.MANIFEST, "[]"),
        (locked_tests.MANIFEST, '{"t": {"assertions": 1}}'),
        (Path(LOCKED), "{"),
        (Path(LOCKED), "def test_x():\n    pass\x00\n"),
    ],
    ids=["manifest json", "manifest list", "manifest fields", "test syntax", "test nul"],
)
def test_an_unreadable_tree_cannot_be_graded(tmp_path, capsys, path, text):
    base = _tree(tmp_path / "base", GUARD)
    head = _tree(tmp_path / "head", GUARD)
    (head / path).write_text(text)
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert out.startswith("::error::")


def test_a_self_referencing_case_list_counts_as_one_case(tmp_path):
    text = GUARD.replace('CASES = [("a", 1), ("b", 2)]', "CASES = []\nCASES = CASES + CASES")
    _tree(tmp_path, text)
    assert locked_tests.load(tmp_path)[f"{LOCKED}::test_blocks"]["cases"] == 1


def test_grading_without_a_base_is_refused():
    with pytest.raises(SystemExit):
        locked_tests.main([])


def test_the_repository_manifest_matches_its_locked_tests():
    measured = locked_tests.measure(_ROOT)
    assert not [key for key, entry in measured.items() if entry["disabled"]]
    assert locked_tests.load(_ROOT) == {
        key: {field: entry[field] for field in locked_tests.FIELDS} for key, entry in measured.items()
    }
    assert {key.split("::")[0] for key in measured} == set(locked_tests.LOCKED)


@pytest.mark.parametrize(
    "locked_base,message",
    [
        (False, f"::error::The locked test {LOCKED}::TestGuard::test_raises is skipped, xfailed or returns early.\n"),
        (True, "::error::The base locks guard tests but dev carries no scripts/locked_tests.py"),
    ],
    ids=["introducing pull request", "dev without the grader"],
)
def test_the_gate_runs_in_lint_and_fails_closed_without_a_trusted_grader(tmp_path, locked_base, message):
    steps = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["lint"]["steps"]
    step = next(step for step in steps if step.get("name") == "Hold the locked guard tests")
    assert step["if"] == "${{ !cancelled() }}"
    (tmp_path / "grader").mkdir()
    if locked_base:
        _tree(tmp_path / "base", GUARD)
    else:
        (tmp_path / "base").mkdir()
    head = _tree(tmp_path / "head", GUARD.replace("    def test_raises", "    @pytest.mark.skip\n    def test_raises"))
    env = dict(
        os.environ,
        RUNNER_TEMP=str(tmp_path),
        GITHUB_WORKSPACE=str(head),
        PATH=f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
    )
    result = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, cwd=_ROOT, capture_output=True, text=True)
    assert result.returncode == 1
    assert message in result.stdout
