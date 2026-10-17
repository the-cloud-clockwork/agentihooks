import json
import subprocess

import pytest

from scripts.swarm import publish_gate

pytestmark = pytest.mark.unit

OFFENDER = "def old(a,b,c,d,e,f,g,h):\n    return a+b\n"
FORMATTED = "def old(a, b, c, d, e, f, g, h):\n    return a + b\n"
SIMPLE = "def new():\n    return 1\n"


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "dev")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.com")
    remote = tmp_path / "remote.git"
    git(root, "init", "-q", "--bare", str(remote))
    git(root, "remote", "add", "origin", str(remote))
    monkeypatch.chdir(root)

    def commit(files, message, push=False):
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git(root, "add", "--", *files)
        git(root, "commit", "-qm", message)
        if push:
            git(root, "push", "-q", "origin", "dev")
            git(root, "checkout", "-q", "-b", "task")

    return commit


def listed(units):
    return {publish_gate.ALLOWLIST: json.dumps(units)}


def test_formatting_an_old_offender_does_not_grade_it(repo, capsys):
    repo({"mod.py": OFFENDER}, "base", push=True)
    repo({"mod.py": FORMATTED + "\n\n" + SIMPLE}, "format and add")
    assert publish_gate.main() == 0
    assert capsys.readouterr().out.endswith("Graded 1 changed Python files\n")


def test_a_touched_unlisted_offender_fails(repo, capsys):
    repo({"mod.py": OFFENDER}, "base", push=True)
    repo({"mod.py": "def old(a, b, c, d, e, f, g, h):\n    return a + h\n"}, "touch")
    assert publish_gate.main() == 1
    assert capsys.readouterr().out.splitlines()[1:] == [
        "mod.py::old breaks PLR0913 at 8; it is not on the base allowlist"
    ]


def test_a_listed_offender_passes_while_it_does_not_grow(repo):
    repo({"mod.py": OFFENDER, **listed({"mod.py::old": {"PLR0913": 8}})}, "base", push=True)
    repo({"mod.py": "def old(a, b, c, d, e, f, g, h):\n    return a + h\n"}, "touch")
    assert publish_gate.main() == 0


def test_a_planted_growth_of_a_listed_offender_fails(repo, capsys):
    repo({"mod.py": OFFENDER, **listed({"mod.py::old": {"PLR0913": 8}})}, "base", push=True)
    repo({"mod.py": "def old(a, b, c, d, e, f, g, h, i):\n    return a + i\n"}, "grow")
    assert publish_gate.main() == 1
    assert capsys.readouterr().out.splitlines()[1:] == ["mod.py::old grew on PLR0913 from 8 to 9"]


def test_an_entry_added_on_the_branch_does_not_count(repo, capsys):
    repo({"mod.py": OFFENDER}, "base", push=True)
    repo(
        {"mod.py": "def old(a, b, c, d, e, f, g, h):\n    return a + h\n", **listed({"mod.py::old": {"PLR0913": 8}})},
        "self list",
    )
    assert publish_gate.main() == 1
    assert capsys.readouterr().out.splitlines()[1:] == [
        "mod.py::old breaks PLR0913 at 8; it is not on the base allowlist"
    ]


def test_an_untouched_offender_beside_a_touched_function_passes(repo):
    repo({"mod.py": FORMATTED + "\n\n" + SIMPLE}, "base", push=True)
    repo({"mod.py": FORMATTED + "\n\n" + "def new():\n    return 2\n"}, "touch neighbour")
    assert publish_gate.main() == 0


def test_a_new_offender_in_a_new_file_fails(repo, capsys):
    repo({"base.py": SIMPLE}, "base", push=True)
    repo({"mod.py": FORMATTED}, "new file")
    assert publish_gate.main() == 1
    assert capsys.readouterr().out.splitlines()[1:] == [
        "mod.py::old breaks PLR0913 at 8; it is not on the base allowlist"
    ]


def test_a_nested_function_is_graded_under_its_qualified_name(repo, capsys):
    repo({"mod.py": SIMPLE}, "base", push=True)
    repo({"mod.py": "class Box:\n    def old(self, a, b, c, d, e, f, g):\n        return a\n"}, "nested")
    assert publish_gate.main() == 1
    assert capsys.readouterr().out.splitlines()[1:] == [
        "mod.py::Box.old breaks PLR0913 at 8; it is not on the base allowlist"
    ]


def test_long_functions_and_files_honour_the_allowlist(repo, capsys):
    body = "def long():\n" + "    print(1)\n" * 3001
    units = {"mod.py": {"file-lines": 3002}, "mod.py::long": {"function-lines": 3002}}
    repo({"mod.py": body, **listed(units)}, "base", push=True)
    repo({"mod.py": body.replace("print(1)", "print(2)", 1)}, "touch")
    assert publish_gate.main() == 0
    repo({"mod.py": body + "    print(3)\n"}, "grow")
    assert publish_gate.main() == 1
    assert capsys.readouterr().out.splitlines()[-2:] == [
        "mod.py grew on file-lines from 3002 to 3003",
        "mod.py::long grew on function-lines from 3002 to 3003",
    ]


def test_an_unparsable_base_file_grades_every_head_function(repo, capsys):
    repo({"mod.py": "broken =\n"}, "base", push=True)
    repo({"mod.py": FORMATTED}, "repair")
    assert publish_gate.main() == 1
    assert capsys.readouterr().out.splitlines()[1:] == [
        "mod.py::old breaks PLR0913 at 8; it is not on the base allowlist"
    ]
