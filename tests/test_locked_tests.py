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


def _command():
    return "x"


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
MANIFEST_TEXT = """{
 "tests/test_secrets.py::TestGuard::test_no_raise": {
  "assertions": 0,
  "calls": 1,
  "cases": 1,
  "texts": []
 },
 "tests/test_secrets.py::TestGuard::test_raises": {
  "assertions": 1,
  "calls": 2,
  "cases": 1,
  "texts": [
   "8af78b2968d96322"
  ]
 },
 "tests/test_secrets.py::_blocked": {
  "assertions": 1,
  "calls": 0,
  "cases": 1,
  "texts": [
   "14bb4618231c149a"
  ]
 },
 "tests/test_secrets.py::test_blocks": {
  "assertions": 2,
  "calls": 1,
  "cases": 2,
  "texts": [
   "6184e37469fc43c5",
   "ed198a9be751aa93"
  ]
 }
}
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


def _measured(tmp_path: Path, text: str) -> dict:
    (tmp_path / LOCKED).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / LOCKED).write_text(text)
    return {key.split("::", 1)[1]: entry for key, entry in locked_tests.measure(tmp_path).items()}


def test_the_unchanged_guard_tests_are_green(tmp_path, capsys):
    base = _tree(tmp_path / "base", GUARD)
    code, out = _grade(base, _tree(tmp_path / "head", GUARD), capsys)
    assert (code, out) == (0, "4 locked tests on the base, 4 measured on the head, 0 errors\n")


def test_the_manifest_is_written_sorted_one_space_indented(tmp_path):
    assert (_tree(tmp_path, GUARD) / locked_tests.MANIFEST).read_text() == MANIFEST_TEXT


def test_the_head_defaults_to_the_working_directory(tmp_path, capsys, monkeypatch):
    base = _tree(tmp_path / "base", GUARD)
    monkeypatch.chdir(_tree(tmp_path / "head", GUARD))
    assert locked_tests.main(["--base", str(base)]) == 0
    assert capsys.readouterr().out == "4 locked tests on the base, 4 measured on the head, 0 errors\n"


@pytest.mark.parametrize(
    "planted",
    [
        GUARD.replace("@pytest.mark.parametrize", "@pytest.mark.skip\n@pytest.mark.parametrize"),
        GUARD.replace("    def test_raises", "    @pytest.mark.xfail\n    def test_raises"),
        GUARD.replace("class TestGuard:\n", "class TestGuard:\n    pytestmark = pytest.mark.skip\n"),
        GUARD.replace("class TestGuard:\n", "class TestGuard:\n    LATER = pytest.mark.skipif(True, reason='x')\n"),
        GUARD.replace("import pytest\n", "import pytest\n\npytestmark = [pytest.mark.skipif(True, reason='x')]\n"),
        GUARD.replace("import pytest\n", "import pytest\n\npytest.skip('x', allow_module_level=True)\n"),
        GUARD.replace('CASES = [("a", 1), ("b", 2)]', 'CASES = [pytest.param("a", 1, marks=pytest.mark.skip)]'),
        GUARD.replace("        str(1)\n", "        pytest.skip('later')\n        str(1)\n"),
        GUARD.replace("import pytest\n", "import pytest as pt\n").replace("        str(1)\n", "        pt.xfail()\n"),
        GUARD.replace("import pytest\n", "import pytest\nfrom pytest import xfail as later\n").replace(
            "        str(1)\n", "        later()\n"
        ),
        GUARD.replace("def _command():\n", "def _gate():\n    pytest.skip('x')\n\n\ndef _command():\n")
        .replace('    return "x"\n', "    _gate()\n")
        .replace("        str(1)\n", "        _command()\n"),
        GUARD.replace("        str(1)\n", "        self._gate()\n").replace(
            "    def test_raises", "    def _gate(self):\n        pytest.importorskip('x')\n\n    def test_raises"
        ),
        GUARD.replace("        str(1)\n", "        request.applymarker(pytest.mark.skip(reason='x'))\n"),
        GUARD.replace("        str(1)\n", "        request.node.add_marker('xfail')\n"),
        GUARD.replace("        str(1)\n", "        pytest.exit('x', returncode=0)\n"),
        GUARD.replace("        str(1)\n", "        self.skipTest('x')\n"),
        GUARD.replace("        str(1)\n", "        raise unittest.SkipTest('x')\n"),
        GUARD.replace("        str(1)\n", "        os._exit(0)\n"),
        GUARD.replace('CASES = [("a", 1), ("b", 2)]', "CASES = []"),
        GUARD.replace("        str(1)\n", "        return\n        str(1)\n"),
        GUARD.replace("        str(1)\n", "        if str(1):\n            return\n"),
        GUARD.replace("    assert command\n", "    if command:\n        return\n    assert command\n"),
        GUARD.replace("    assert command\n", "    return; assert command\n"),
    ],
    ids=[
        "skip",
        "xfail",
        "class mark",
        "class stored mark",
        "module mark",
        "module skip",
        "skipped case",
        "skip call",
        "aliased pytest",
        "imported alias",
        "skipping helper chain",
        "skipping method",
        "applied marker",
        "string marker",
        "exit",
        "unittest skip",
        "unittest SkipTest",
        "process exit",
        "empty cases",
        "early return",
        "nested return",
        "helper early return",
        "helper return on the assertion line",
    ],
)
def test_a_skipped_or_xfailed_locked_test_is_red(tmp_path, capsys, planted):
    base = _tree(tmp_path / "base", GUARD)
    head = _tree(tmp_path / "head", planted, record=GUARD)
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert " is skipped, xfailed or returns early.\n" in out


def test_returns_inside_nested_definitions_do_not_disable_a_test(tmp_path):
    text = GUARD.replace("        str(1)\n", "        def inner():\n            return 1\n        str(inner())\n")
    text = text.replace('            int("x")\n', "            int((lambda: 'x')())\n")
    measured = _measured(tmp_path, text)
    assert not any(entry["disabled"] for entry in measured.values())

@pytest.mark.parametrize(
    "body,exit",
    [
        ("    for item in command:\n        assert item\n", "        continue\n        assert item\n"),
        ("    for item in command:\n        assert item\n", "        break\n        assert item\n"),
        ("    for item in command:\n        assert item\n", "        assert item\n        return\n"),
        ("    for item in command:\n        assert item\n", "        assert item\n        break\n"),
        ("    while command:\n        assert command\n", "        assert command\n        return\n"),
    ],
    ids=["continue", "break", "late return", "late break", "while return"],
)
def test_loop_exits_that_skip_locked_assertions_are_red(tmp_path, capsys, body, exit):
    clean = GUARD.replace("    assert command\n", body)
    planted = clean.replace(body, body.splitlines(keepends=True)[0] + exit)
    base = _tree(tmp_path / "base", clean)
    head = _tree(tmp_path / "head", planted, record=clean)
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert f"The locked test {LOCKED}::_blocked is skipped, xfailed or returns early.\n" in out


@pytest.mark.parametrize(
    "body",
    [
        "    for item in command:\n        assert item\n        continue\n",
        "    for item in command:\n        assert item\n    return command\n",
        "    assert command\n    for item in command:\n        break\n",
        "    for item in command:\n        for other in item:\n            break\n        assert item\n",
        "    for item in command:\n        assert item\n    else:\n        return command\n",
    ],
    ids=["completed iteration", "completed loop", "completed assertion", "inner break", "loop else"],
)
def test_loop_exits_after_all_locked_assertions_stay_green(tmp_path, body):
    text = GUARD.replace("    assert command\n", body)
    assert not _measured(tmp_path, text)["_blocked"]["disabled"]


@pytest.mark.parametrize("decorator", ["@pytest.mark.skip", "@pytest.mark.parametrize('x', [])"])
def test_a_new_skipped_test_in_a_locked_file_is_red(tmp_path, capsys, decorator):
    base = _tree(tmp_path / "base", GUARD)
    head = _tree(tmp_path / "head", GUARD + f"\n\n{decorator}\ndef test_new():\n    assert True\n")
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert f"::error::The locked test {LOCKED}::test_new is skipped, xfailed or returns early.\n" in out


def test_a_deleted_locked_test_is_red(tmp_path, capsys):
    base = _tree(tmp_path / "base", GUARD)
    head = _tree(tmp_path / "head", GUARD.replace("test_no_raise", "check_no_raise"))
    code, out = _grade(base, head, capsys)
    key = f"{LOCKED}::TestGuard::test_no_raise"
    assert code == 1
    assert out.splitlines()[1:] == [
        f"::error::The locked test {key} is deleted or renamed.",
        *(
            f"::error::The head {locked_tests.MANIFEST} drops or lowers the locked {field} of {key}."
            for field in locked_tests.FIELDS
        ),
        f"::error::The head {locked_tests.MANIFEST} drops or changes the locked assertion texts of {key}.",
    ]


@pytest.mark.parametrize(
    "planted,message",
    [
        (GUARD.replace("    assert code\n", ""), "test_blocks has 1 assertions, the base locks 2."),
        (GUARD.replace("    assert command\n", "    pass\n"), "_blocked is deleted or renamed."),
        (GUARD.replace(', ("b", 2)', ""), "test_blocks has 1 cases, the base locks 2."),
        (
            GUARD.replace("        str(1)\n", "        pass\n"),
            "TestGuard::test_no_raise has 0 calls, the base locks 1.",
        ),
        (
            GUARD.replace("    assert code\n", "    assert True\n"),
            "test_blocks changes or drops an assertion the base locks.",
        ),
        (
            GUARD.replace("    assert command\n", "    assert command or True\n"),
            "_blocked changes or drops an assertion the base locks.",
        ),
    ],
    ids=["assert", "helper", "case", "call", "assert true swap", "helper assert text"],
)
def test_a_weakened_locked_test_is_red_even_when_rerecorded(tmp_path, capsys, planted, message):
    base = _tree(tmp_path / "base", GUARD)
    code, out = _grade(base, _tree(tmp_path / "head", planted), capsys)
    assert code == 1
    assert f"::error::The locked test {LOCKED}::" + message in out


def test_a_manifest_recording_more_than_the_head_measures_is_red(tmp_path, capsys):
    base = _tree(tmp_path / "base", GUARD)
    head = _tree(tmp_path / "head", GUARD)
    data = locked_tests.load(head)
    data[f"{LOCKED}::TestGuard::test_raises"]["calls"] = 3
    (head / locked_tests.MANIFEST).write_text(json.dumps(data))
    code, out = _grade(base, head, capsys)
    assert (code, out.splitlines()[1:]) == (
        1,
        [
            f"::error::The head {locked_tests.MANIFEST} records more than {LOCKED}::TestGuard::test_raises measures;"
            " run python -m scripts.locked_tests --write."
        ],
    )


@pytest.mark.parametrize(
    "texts,message",
    [
        ([], "drops or changes the locked assertion texts of"),
        (["8af78b2968d96322", "0000000000000000"], "records more than"),
    ],
    ids=["dropped", "unmeasured"],
)
def test_a_manifest_text_the_base_or_head_disagrees_with_is_red(tmp_path, capsys, texts, message):
    base = _tree(tmp_path / "base", GUARD)
    head = _tree(tmp_path / "head", GUARD)
    data = locked_tests.load(head)
    data[f"{LOCKED}::TestGuard::test_raises"]["texts"] = texts
    (head / locked_tests.MANIFEST).write_text(json.dumps(data))
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert [line for line in out.splitlines()[1:] if message in line] == out.splitlines()[1:]
    assert f"{LOCKED}::TestGuard::test_raises" in out


def test_an_added_assertion_keeps_the_locked_texts_green(tmp_path, capsys):
    base = _tree(tmp_path / "base", GUARD)
    head = _tree(tmp_path / "head", GUARD.replace("    assert code\n", "    assert code\n    assert code > 0\n"))
    assert _grade(base, head, capsys)[0] == 0


def test_a_base_manifest_without_texts_grades_by_counts(tmp_path, capsys):
    base = _tree(tmp_path / "base", GUARD)
    data = locked_tests.load(base)
    for entry in data.values():
        del entry["texts"]
    (base / locked_tests.MANIFEST).write_text(json.dumps(data))
    head = _tree(tmp_path / "head", GUARD.replace("    assert code\n", "    assert True\n"))
    assert _grade(base, head, capsys)[0] == 0


def test_a_helper_returning_after_its_assertion_is_not_disabled(tmp_path):
    text = GUARD.replace("    assert command\n", "    assert command\n    return command\n")
    assert not _measured(tmp_path, text)["_blocked"]["disabled"]


def test_a_helper_returning_its_assertion_is_not_disabled(tmp_path):
    text = GUARD.replace("    assert command\n", "    return command.assert_called_once()\n")
    assert not _measured(tmp_path, text)["_blocked"]["disabled"]


def test_only_checks_assert_methods_and_asserting_helpers_count_as_assertions(tmp_path):
    text = GUARD.replace(
        "        str(1)\n", "        assertion_noop()\n        mock.assert_called_once()\n        _blocked(1)\n"
    )
    assert _measured(tmp_path, text)["TestGuard::test_no_raise"]["assertions"] == 2


@pytest.mark.parametrize(
    "values,cases",
    [
        ("argvalues=[1, 2, 3]", 3),
        ("A + B", 3),
        ("[1, 2] + list(B)", 1),
        ("list(A) + B", 1),
        ("list(A)", 1),
        ("C", 1),
        ("[]", 0),
    ],
)
def test_parametrized_cases_resolve_literal_lists(tmp_path, values, cases):
    text = (
        "import pytest\n\nA = [1, 2]\nB = (3,)\nC = []\nC = C + C\n\n\n"
        f"@pytest.mark.parametrize('a', {values})\ndef test_a(a):\n    assert a\n"
    )
    assert _measured(tmp_path, text)["test_a"]["cases"] == cases


def test_every_locked_file_is_measured_when_one_is_missing(tmp_path):
    (tmp_path / locked_tests.LOCKED[1]).parent.mkdir(parents=True)
    (tmp_path / locked_tests.LOCKED[1]).write_text("def test_a():\n    assert 1\n")
    assert list(locked_tests.measure(tmp_path)) == [f"{locked_tests.LOCKED[1]}::test_a"]


@pytest.mark.parametrize(
    "config",
    [
        "conftest.py",
        "pyproject.toml",
        "pytest.ini",
        "setup.cfg",
        "tox.ini",
        "tests/conftest.py",
        "tests/context/conftest.py",
        "tests/pyproject.toml",
        "tests/pytest.ini",
        "tests/setup.cfg",
        ".github/workflows/test.yml",
        ".github/actions/run/action.yaml",
    ],
)
def test_a_config_naming_a_locked_module_is_red(tmp_path, capsys, config):
    base = _tree(tmp_path / "base", GUARD)
    head = _tree(tmp_path / "head", GUARD)
    (head / config).parent.mkdir(parents=True, exist_ok=True)
    (head / config).write_text(f'collect_ignore = ["{Path(LOCKED).name}"]\n')
    code, out = _grade(base, head, capsys)
    assert (code, out.splitlines()[1:]) == (
        1,
        [
            f"::error::{config} names the locked module {Path(LOCKED).stem};"
            " a locked test cannot be ignored or deselected."
        ],
    )


def test_a_config_naming_no_locked_module_is_green(tmp_path, capsys):
    base = _tree(tmp_path / "base", GUARD)
    head = _tree(tmp_path / "head", GUARD)
    (head / "tests/conftest.py").write_text("collect_ignore = []\n")
    assert _grade(base, head, capsys)[0] == 0


def test_a_base_without_a_manifest_locks_the_head(tmp_path, capsys):
    base = tmp_path / "base"
    base.mkdir()
    code, out = _grade(base, _tree(tmp_path / "head", GUARD), capsys)
    assert code == 0
    assert out.startswith(f"::notice::The base has no {locked_tests.MANIFEST}; the head manifest locks itself.\n")


@pytest.mark.parametrize(
    "path,text,message",
    [
        (None, None, f"::error::the head has no {locked_tests.MANIFEST}\n"),
        (locked_tests.MANIFEST, "{", f"::error::{locked_tests.MANIFEST} is not valid JSON: Expecting"),
        (
            locked_tests.MANIFEST,
            "[]",
            f"::error::{locked_tests.MANIFEST} must map each locked test to its assertions, calls and cases\n",
        ),
        (
            locked_tests.MANIFEST,
            '{"t": {"assertions": 1, "calls": 1}}',
            f"::error::{locked_tests.MANIFEST} must map each locked test to its assertions, calls and cases\n",
        ),
        (
            locked_tests.MANIFEST,
            '{"t": {"assertions": 1, "calls": 1, "cases": 1, "texts": [1]}}',
            f"::error::{locked_tests.MANIFEST} must map each locked test to its assertions, calls and cases\n",
        ),
        (Path(LOCKED), "{", f"::error::{LOCKED} does not parse: "),
        (Path(LOCKED), "def test_x():\n    pass\x00\n", "::error::"),
    ],
    ids=[
        "no manifest",
        "manifest json",
        "manifest list",
        "manifest fields",
        "manifest texts",
        "test syntax",
        "test nul",
    ],
)
def test_an_unreadable_tree_cannot_be_graded(tmp_path, capsys, path, text, message):
    base = _tree(tmp_path / "base", GUARD)
    head = _tree(tmp_path / "head", GUARD)
    if path is None:
        (head / locked_tests.MANIFEST).unlink()
    else:
        (head / path).write_text(text)
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert out.startswith(message)


def test_a_syntax_error_names_the_locked_file(tmp_path, capsys):
    base = _tree(tmp_path / "base", GUARD)
    head = _tree(tmp_path / "head", GUARD)
    (head / LOCKED).write_text("def (:\n")
    code, out = _grade(base, head, capsys)
    assert (code, out) == (1, f"::error::{LOCKED} does not parse: invalid syntax ({Path(LOCKED).name}, line 1)\n")


def test_grading_without_a_base_is_refused(capsys):
    with pytest.raises(SystemExit) as raised:
        locked_tests.main([])
    assert raised.value.code == 2
    assert capsys.readouterr().err.endswith(": error: grading needs --base\n")


def test_the_repository_manifest_matches_its_locked_tests():
    measured = locked_tests.measure(_ROOT)
    assert not [key for key, entry in measured.items() if entry["disabled"]]
    assert locked_tests.load(_ROOT) == {
        key: {field: entry[field] for field in (*locked_tests.FIELDS, locked_tests.TEXTS)}
        for key, entry in measured.items()
    }
    assert {key.split("::")[0] for key in measured} == set(locked_tests.LOCKED)
    assert locked_tests.unlocking_configs(_ROOT) == []


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
