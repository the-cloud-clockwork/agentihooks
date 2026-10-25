import json
import subprocess
from pathlib import Path

import pytest

from scripts import dead_code, size_limits

pytestmark = pytest.mark.unit

USED = "def used():\n    return 1\n\n\nprint(used())\n"
PLANTED = USED + "\n\ndef planted():\n    return 2\n"


def _tree(root: Path, files: dict[str, str]) -> Path:
    (root / "tests").mkdir(parents=True)
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-f", *files], check=True)
    return root


def _recorded(root: Path, files: dict[str, str]) -> Path:
    tree = _tree(root, files)
    assert dead_code.main(["--write", "--head", str(tree)]) == 0
    return tree


def _grade(base: Path, head: Path, capsys) -> tuple[int, str]:
    code = dead_code.main(["--base", str(base), "--head", str(head)])
    return code, capsys.readouterr().out


def test_a_planted_unused_function_in_a_hook_is_red(tmp_path, capsys):
    base = _recorded(tmp_path / "base", {"hooks/guard.py": USED})
    head = _tree(tmp_path / "head", {"hooks/guard.py": PLANTED})
    (head / dead_code.ALLOWLIST).write_text((base / dead_code.ALLOWLIST).read_text())
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "1 files hold dead code, 0 on the base allowlist, 3 errors" in out
    assert "::error::hooks/guard.py has 1 unused names; the base allows 0\n" in out
    assert "::error::hooks/guard.py:8: unused function 'planted' (60% confidence)\n" in out
    assert (
        "::error::tests/DEAD_CODE_ALLOWLIST.json records 0 for hooks/guard.py, below the head's 1;"
        " run python -m scripts.dead_code --write\n"
    ) in out


def test_a_pull_request_cannot_allowlist_its_own_dead_code(tmp_path, capsys):
    base = _recorded(tmp_path / "base", {"scripts/tool.py": USED})
    head = _recorded(tmp_path / "head", {"scripts/tool.py": PLANTED})
    assert dead_code.load(head) == {"scripts/tool.py": 1}
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "scripts/tool.py has 1 unused names; the base allows 0" in out
    assert "tests/DEAD_CODE_ALLOWLIST.json records 1 for scripts/tool.py, above the base allowlist's 0" in out


def test_only_hooks_and_scripts_are_graded(tmp_path):
    tree = _tree(tmp_path, {"hooks/a.py": PLANTED, "scripts/b.py": PLANTED, "tests/c.py": PLANTED, "d.py": PLANTED})
    assert dead_code.measure(tree) == {"hooks/a.py": 1, "scripts/b.py": 1}


def test_a_name_used_in_another_graded_file_is_live(tmp_path):
    tree = _tree(tmp_path, {"hooks/a.py": PLANTED, "scripts/b.py": "from hooks.a import planted\n\nplanted()\n"})
    assert dead_code.measure(tree) == {}


def test_a_name_used_only_outside_hooks_and_scripts_is_dead(tmp_path):
    tree = _tree(tmp_path, {"hooks/a.py": PLANTED, "tests/test_a.py": "from hooks.a import planted\n\nplanted()\n"})
    assert dead_code.measure(tree) == {"hooks/a.py": 1}


def test_an_allowlisted_file_may_stay_but_not_grow(tmp_path, capsys):
    base = _recorded(tmp_path / "base", {"hooks/guard.py": PLANTED})
    head = _recorded(tmp_path / "head", {"hooks/guard.py": PLANTED + "\n\ndef other():\n    return 3\n"})
    code, out = _grade(base, base, capsys)
    assert code == 0, out
    assert "1 files hold dead code, 1 on the base allowlist, 0 errors" in out
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "hooks/guard.py has 2 unused names; the base allows 1" in out


def test_a_shrunk_file_may_keep_its_recorded_count_until_written(tmp_path, capsys):
    base = _recorded(tmp_path / "base", {"hooks/guard.py": PLANTED})
    head = _tree(tmp_path / "head", {"hooks/guard.py": USED})
    (head / dead_code.ALLOWLIST).write_text((base / dead_code.ALLOWLIST).read_text())
    code, out = _grade(base, head, capsys)
    assert code == 0, out
    assert "0 files hold dead code, 1 on the base allowlist, 0 errors" in out
    assert "::notice::tests/DEAD_CODE_ALLOWLIST.json allows 1 more than the head holds;" in out
    dead_code.main(["--write", "--head", str(head)])
    code, out = _grade(base, head, capsys)
    assert code == 0
    assert "::notice::" not in out
    assert dead_code.load(head) == {}


def test_a_shrink_landed_twice_leaves_the_next_head_green(tmp_path, capsys):
    dead = PLANTED + "\n\ndef other():\n    return 3\n"
    base = _recorded(tmp_path / "base", {"hooks/guard.py": dead})
    assert dead_code.load(base) == {"hooks/guard.py": 2}
    head = _tree(tmp_path / "head", {"hooks/guard.py": USED})
    (head / dead_code.ALLOWLIST).write_text(json.dumps({"hooks/guard.py": 1}))
    code, out = _grade(base, head, capsys)
    assert code == 0, out


def test_noqa_and_config_in_the_graded_tree_hide_nothing(tmp_path):
    tree = _tree(
        tmp_path,
        {
            "hooks/guard.py": PLANTED.replace("def planted():", "def planted():  # noqa")
            + "\n\nimport os  # noqa: F401 kept for side effects\n\n\ndef kept():  # noqa: F401\n    return 4\n",
            "pyproject.toml": '[tool.vulture]\nexclude = ["hooks"]\nignore_names = ["planted"]\n',
        },
    )
    assert dead_code.measure(tree) == {"hooks/guard.py": 3}


def test_a_file_cannot_grow_back_into_slack_the_base_still_records(tmp_path, capsys):
    base = _tree(tmp_path / "base", {"hooks/guard.py": USED})
    (base / dead_code.ALLOWLIST).write_text(json.dumps({"hooks/guard.py": 1}))
    head = _recorded(tmp_path / "head", {"hooks/guard.py": PLANTED})
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "::error::hooks/guard.py has 1 unused names; the base allows 0\n" in out
    assert "records 1 for hooks/guard.py, above" not in out


@pytest.mark.parametrize("text", ["[]", '{"hooks/guard.py": "1"}', '{"hooks/guard.py": true}'])
def test_an_allowlist_without_integer_counts_cannot_grade(tmp_path, capsys, text):
    base = _recorded(tmp_path / "base", {"hooks/guard.py": USED})
    head = _recorded(tmp_path / "head", {"hooks/guard.py": USED})
    (base / dead_code.ALLOWLIST).write_text(text)
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert out == f"::error::{base / dead_code.ALLOWLIST} holds no object of per file counts\n"


def test_a_file_the_base_measures_but_never_recorded_has_no_allowance(tmp_path, capsys):
    base = _tree(tmp_path / "base", {"hooks/guard.py": PLANTED})
    (base / dead_code.ALLOWLIST).write_text("{}")
    head = _recorded(tmp_path / "head", {"hooks/guard.py": PLANTED})
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "::error::hooks/guard.py has 1 unused names; the base allows 0\n" in out


def test_the_slack_notice_counts_what_the_head_still_holds(tmp_path, capsys):
    base = _recorded(tmp_path / "base", {"hooks/guard.py": PLANTED + "\n\ndef other():\n    return 3\n"})
    head = _tree(tmp_path / "head", {"hooks/guard.py": PLANTED})
    (head / dead_code.ALLOWLIST).write_text((base / dead_code.ALLOWLIST).read_text())
    code, out = _grade(base, head, capsys)
    assert code == 0, out
    assert "::notice::tests/DEAD_CODE_ALLOWLIST.json allows 1 more than the head holds;" in out


def test_an_unreadable_file_cannot_be_graded(tmp_path):
    tree = _tree(tmp_path, {"hooks/guard.py": USED})
    (tree / "hooks/guard.py").write_bytes(b"x = '\xff'\n")
    with pytest.raises(size_limits.GradeError, match=r"^vulture cannot read /.+/hooks/guard\.py: $"):
        dead_code.measure(tree)


def test_unused_imports_and_attributes_are_counted(tmp_path):
    tree = _tree(tmp_path, {"hooks/guard.py": PLANTED + "\n\nimport os\n"})
    assert dead_code.measure(tree) == {"hooks/guard.py": 2}
    boxed = USED + "\n\nclass Box:\n    def __init__(self):\n        self.lid = 1\n\n\nBox()\n"
    tree = _tree(tmp_path / "attr", {"hooks/guard.py": boxed})
    assert dead_code.measure(tree) == {"hooks/guard.py": 1}


def test_test_names_in_a_test_named_path_are_still_counted(tmp_path):
    dead = USED + "\n\ndef test_helper():\n    return 2\n\n\nclass TestBox:\n    pass\n"
    tree = _tree(tmp_path / "tests" / "checkout", {"hooks/test_guard.py": dead})
    assert dead_code.measure(tree) == {"hooks/test_guard.py": 2}


def test_an_untracked_file_is_not_graded(tmp_path):
    tree = _tree(tmp_path, {"hooks/guard.py": USED})
    (tree / "hooks/stray.py").write_text(PLANTED)
    assert dead_code.measure(tree) == {}


def test_measurement_resolves_a_symlinked_tree(tmp_path):
    tree = _tree(tmp_path / "tree", {"hooks/guard.py": PLANTED})
    (tmp_path / "link").symlink_to(tree)
    assert dead_code.measure(tmp_path / "link") == {"hooks/guard.py": 1}


def test_the_allowlist_is_written_sorted_one_space_indented(tmp_path):
    files = {"hooks/b.py": PLANTED, "hooks/a.py": PLANTED.replace("planted", "spare")}
    tree = _recorded(tmp_path, files | {"hooks/C.py": PLANTED.replace("planted", "third")})
    # Vulture orders by lowercased path; the allowlist orders by exact key, so C sorts first.
    assert (tree / dead_code.ALLOWLIST).read_text() == '{\n "hooks/C.py": 1,\n "hooks/a.py": 1,\n "hooks/b.py": 1\n}\n'


@pytest.mark.parametrize("side", ["base", "head"])
def test_a_corrupt_allowlist_cannot_grade(tmp_path, capsys, side):
    trees = {name: _recorded(tmp_path / name, {"hooks/guard.py": USED}) for name in ("base", "head")}
    (trees[side] / dead_code.ALLOWLIST).write_text("{")
    code, out = _grade(trees["base"], trees["head"], capsys)
    assert code == 1
    assert out.startswith("::error::Expecting property name enclosed in double quotes")


def test_a_base_without_an_allowlist_is_red(tmp_path, capsys):
    base = _tree(tmp_path / "base", {"hooks/guard.py": USED})
    head = _recorded(tmp_path / "head", {"hooks/guard.py": USED})
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "::error::the base has no tests/DEAD_CODE_ALLOWLIST.json, so the head cannot be graded" in out


def test_a_tree_without_hooks_or_scripts_cannot_be_graded(tmp_path, capsys):
    tree = _tree(tmp_path, {"other/tool.py": PLANTED})
    with pytest.raises(size_limits.GradeError, match="^no tracked Python files under hooks/ or scripts/ in "):
        dead_code.measure(tree)
    assert dead_code.main(["--write", "--head", str(tree)]) == 1
    assert "::error::no tracked Python files under hooks/ or scripts/" in capsys.readouterr().out


def test_a_head_that_does_not_parse_cannot_be_graded(tmp_path, capsys):
    base = _recorded(tmp_path / "base", {"hooks/guard.py": USED})
    head = _tree(tmp_path / "head", {"hooks/guard.py": "def broken(:\n"})
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "::error::vulture cannot parse every graded file in " in out


def test_the_head_defaults_to_the_working_directory(tmp_path, monkeypatch):
    tree = _tree(tmp_path, {"hooks/guard.py": PLANTED})
    monkeypatch.chdir(tree)
    assert dead_code.main(["--write"]) == 0
    assert json.loads((tree / dead_code.ALLOWLIST).read_text()) == {"hooks/guard.py": 1}


def test_grading_without_a_base_is_refused(tmp_path, capsys):
    with pytest.raises(SystemExit) as raised:
        dead_code.main(["--head", str(tmp_path)])
    assert raised.value.code == 2
    assert capsys.readouterr().err.endswith(": error: grading needs --base\n")


def test_the_grader_refuses_another_vulture_version(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(dead_code.vulture, "__version__", "0.0.1")
    assert dead_code.main(["--write", "--head", str(_tree(tmp_path, {"hooks/guard.py": USED}))]) == 1
    out = capsys.readouterr().out
    assert out == f"::error::The dead code gate needs vulture {dead_code.VULTURE_VERSION}, found 0.0.1.\n"
