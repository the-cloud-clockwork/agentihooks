import json
import subprocess

import pytest

from scripts.swarm import publish_gate

pytestmark = pytest.mark.unit

NUMBERS = [str(n) * 10 for n in range(1, 9)]
LONG = "def long():\n" + "    print(1)\n" * 300 + "    values = [{values}]\n    return values\n"
UNFORMATTED = LONG.format(values=", ".join(NUMBERS))
FORMATTED = LONG.replace("    values = [{values}]\n", "    values = [\n{values}    ]\n").format(
    values="".join(f"        {n},\n" for n in NUMBERS)
)
WIDE = "def {name}(a, b, c, d, e, f, g, h{extra}):\n    return a + {last}\n"
ARGUMENTS = "Too many arguments in function definition"


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "dev")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "init", "-q", "--bare", str(tmp_path / "remote.git"))
    git(root, "remote", "add", "origin", str(tmp_path / "remote.git"))
    monkeypatch.chdir(root)

    def commit(files, push=False):
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        git(root, "add", "--", *files)
        git(root, "commit", "-qm", "change")
        if push:
            git(root, "push", "-q", "origin", "dev")
            git(root, "checkout", "-q", "-b", "task")

    return commit


def listed(units):
    return {publish_gate.size_limits.ALLOWLIST.as_posix(): json.dumps(units)}


def wide(name="old", extra="", last="h"):
    return WIDE.format(name=name, extra=extra, last=last)


def report(capsys):
    return capsys.readouterr().out.splitlines()[1:]


def test_formatting_an_untouched_offender_does_not_grade_it(repo, capsys):
    repo({"mod.py": UNFORMATTED}, push=True)
    repo({"mod.py": FORMATTED})
    assert publish_gate.main() == 0
    assert report(capsys) == ["Graded 1 changed Python files"]


def test_an_inline_suppression_does_not_hide_a_new_offender(repo, capsys):
    repo({"base.py": "value = 1\n"}, push=True)
    repo({"mod.py": wide().replace("):\n", "):  # noqa: PLR0913\n", 1)})
    assert publish_gate.main() == 1
    assert report(capsys) == [f"mod.py: old PLR0913 {ARGUMENTS} (8 > 7)"]


def test_a_listed_unit_without_a_baseline_passes_while_it_does_not_grow(repo):
    repo({"mod.py": wide(), **listed({"mod.py::new": {"PLR0913": 8}})}, push=True)
    repo({"mod.py": wide(name="new")})
    assert publish_gate.main() == 0


def test_a_planted_growth_of_a_listed_unit_fails(repo, capsys):
    repo({"mod.py": wide(), **listed({"mod.py::new": {"PLR0913": 8}})}, push=True)
    repo({"mod.py": wide(name="new", extra=", i", last="i")})
    assert publish_gate.main() == 1
    assert report(capsys) == ["mod.py: new PLR0913 grew from 8 to 9"]


def test_a_shrunk_allowlist_entry_caps_the_baseline(repo, capsys):
    base = wide(extra=", i")
    repo({"mod.py": base, **listed({"mod.py::old": {"PLR0913": 8}})}, push=True)
    repo({"mod.py": base.replace("a + h", "a + i")})
    assert publish_gate.main() == 1
    assert report(capsys) == ["mod.py: old PLR0913 grew from 8 to 9"]


def test_long_units_read_their_allowlist_rules(repo, capsys):
    units = {"mod.py": {"file-lines": 3000}, "mod.py::long": {"function-lines": 301}}
    source = "def old():\n" + "    print(1)\n" * 300 + "\n\n" + "print(1)\n" * 2698
    repo({"mod.py": source, **listed(units)}, push=True)
    repo({"mod.py": source.replace("def old", "def long")})
    assert publish_gate.main() == 1
    assert report(capsys) == ["mod.py: file lines grew from 3000 to 3001"]


def test_a_listed_function_that_grows_its_length_fails(repo, capsys):
    source = "def old():\n" + "    print(1)\n" * 300
    repo({"mod.py": source, **listed({"mod.py::long": {"function-lines": 301}})}, push=True)
    repo({"mod.py": source.replace("def old", "def long") + "    print(1)\n"})
    assert publish_gate.main() == 1
    assert report(capsys) == ["mod.py: long lines grew from 301 to 302"]


def test_an_unlisted_unit_does_not_stop_the_listed_units_after_it(repo, capsys):
    repo({"mod.py": wide(), **listed({"mod.py::new": {"PLR0913": 8}})}, push=True)
    repo({"mod.py": wide(name="first", extra=", i", last="i") + "\n\n" + wide(name="new")})
    assert publish_gate.main() == 1
    assert report(capsys) == [f"mod.py: first PLR0913 {ARGUMENTS} (9 > 7)"]


def test_an_entry_added_on_the_branch_does_not_count(repo, capsys):
    repo({"mod.py": wide()}, push=True)
    repo({"mod.py": wide(name="new"), **listed({"mod.py::new": {"PLR0913": 8}})})
    assert publish_gate.main() == 1
    assert report(capsys) == [f"mod.py: new PLR0913 {ARGUMENTS} (8 > 7)"]


def test_an_old_lint_offender_outside_the_diff_passes(repo, capsys):
    repo({"mod.py": 'value = 1\nlabel = f"plain"\n'}, push=True)
    repo({"mod.py": 'value = 2\nlabel = f"plain"\n'})
    assert publish_gate.main() == 0
    assert report(capsys) == ["Graded 1 changed Python files"]


def test_a_lint_offender_on_a_changed_line_fails(repo, capsys):
    repo({"mod.py": 'label = f"plain"\nvalue = 1\n'}, push=True)
    repo({"mod.py": 'label = f"plain"\nimport os\nvalue = 2\n'})
    assert publish_gate.main() == 1
    assert report(capsys) == [
        "mod.py:2:1: E402 Module level import not at top of file",
        "mod.py:2:8: F401 `os` imported but unused",
    ]


def test_a_suppression_does_not_hide_a_lint_finding_on_a_changed_line(repo, capsys):
    repo({"mod.py": 'label = f"plain"  # noqa: F541\nvalue = 1\n'}, push=True)
    repo({"mod.py": 'label = f"plain"  # noqa: F541\nvalue = 2\nother = f"plain"  # noqa: F541\n'})
    assert publish_gate.main() == 1
    assert report(capsys) == ["mod.py:3:9: F541 f-string without any placeholders"]


def test_a_renamed_file_grades_only_its_changed_lines(repo, capsys):
    source = "import os\n" + "value = 1\n" * 5
    repo({"old.py": source}, push=True)
    git(".", "mv", "old.py", "new.py")
    repo({"new.py": source + 'label = f"plain"\n'})
    assert publish_gate.main() == 1
    assert report(capsys) == ["new.py:7:9: F541 f-string without any placeholders"]


def test_a_syntax_error_outside_the_diff_still_fails(repo, capsys):
    repo({"mod.py": "broken =\n\n\nvalue = 1\n"}, push=True)
    repo({"mod.py": "broken =\n\n\nvalue = 2\n"})
    assert publish_gate.main() == 1
    assert report(capsys)[0].startswith("mod.py:1:9: invalid-syntax ")


def test_a_ruff_that_cannot_run_fails(repo, capsys):
    repo({"pyproject.toml": '[tool.ruff]\nline-length = "wide"\n'}, push=True)
    repo({"mod.py": "value = 1\n"})
    assert publish_gate.main() == 1
    assert report(capsys) == ["ruff check exited 2"]


def test_an_old_unformatted_line_outside_the_diff_passes(repo, capsys):
    repo({"mod.py": "value = 1\n\n\n\n\nlabel=1\n"}, push=True)
    repo({"mod.py": "value = 2\n\n\n\n\nlabel=1\n"})
    assert publish_gate.main() == 0
    assert report(capsys) == ["Graded 1 changed Python files"]


def test_an_unformatted_changed_line_fails(repo, capsys):
    repo({"mod.py": "value = 1\n\n\n\n\nlabel=1\n"}, push=True)
    repo({"mod.py": "value = 1\nother=2\n\n\n\nlabel=1\n"})
    assert publish_gate.main() == 1
    assert report(capsys) == ["mod.py:2: would reformat a changed line"]


def test_a_missing_blank_line_before_a_changed_line_fails(repo, capsys):
    repo({"mod.py": "value = 1\n"}, push=True)
    repo({"mod.py": "value = 1\ndef chosen():\n    pass\n"})
    assert publish_gate.main() == 1
    assert report(capsys) == ["mod.py:2: would reformat a changed line"]


def test_an_unformatted_changed_line_after_an_old_one_fails(repo, capsys):
    repo({"mod.py": "a=1\nb = 1\n"}, push=True)
    repo({"mod.py": "a=1\nb=2\n"})
    assert publish_gate.main() == 1
    assert report(capsys) == ["mod.py:2: would reformat a changed line"]
