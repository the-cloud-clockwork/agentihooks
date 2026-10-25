import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import size_limits, swallowed_exceptions

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit

SWALLOWED = "def guard():\n    try:\n        return 1\n    except Exception:\n        pass\n"
NARROW = "def guard():\n    try:\n        return 1\n    except ValueError:\n        return 0\n"


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
    assert swallowed_exceptions.main(["--write", "--head", str(tree)]) == 0
    return tree


def _grade(base: Path, head: Path, capsys) -> tuple[int, str]:
    code = swallowed_exceptions.main(["--base", str(base), "--head", str(head)])
    return code, capsys.readouterr().out


def test_a_planted_swallowed_exception_in_a_hook_is_red(tmp_path, capsys):
    base = _recorded(tmp_path / "base", {"hooks/guard.py": NARROW})
    head = _tree(tmp_path / "head", {"hooks/guard.py": SWALLOWED})
    (head / swallowed_exceptions.ALLOWLIST).write_text((base / swallowed_exceptions.ALLOWLIST).read_text())
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "1 files swallow exceptions, 0 on the base allowlist, 2 errors" in out
    assert "::error::hooks/guard.py has 1 BLE001 hits; the base allowlist allows 0\n" in out
    assert "differs from the head's measurement; run python -m scripts.swallowed_exceptions --write" in out


def test_a_pull_request_cannot_allowlist_its_own_swallow(tmp_path, capsys):
    base = _recorded(tmp_path / "base", {"scripts/gates/gate.py": NARROW})
    head = _recorded(tmp_path / "head", {"scripts/gates/gate.py": SWALLOWED})
    assert swallowed_exceptions.load(head) == {"scripts/gates/gate.py": {"BLE001": 1}}
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "scripts/gates/gate.py has 1 BLE001 hits; the base allowlist allows 0" in out


def test_only_hooks_and_gates_are_graded(tmp_path):
    tree = _tree(
        tmp_path,
        {"hooks/guard.py": SWALLOWED, "scripts/gates/gate.py": SWALLOWED, "scripts/other.py": SWALLOWED},
    )
    assert swallowed_exceptions.measure(tree) == {
        "hooks/guard.py": {"BLE001": 1},
        "scripts/gates/gate.py": {"BLE001": 1},
    }


def test_useless_try_and_error_logging_in_a_handler_are_counted(tmp_path):
    source = (
        "import logging\n"
        "def guard():\n    try:\n        return 1\n    except Exception:\n        raise\n"
        "def log():\n    try:\n        return 1\n    except ValueError:\n        logging.error('lost')\n"
    )
    tree = _tree(tmp_path, {"hooks/guard.py": source})
    assert swallowed_exceptions.measure(tree) == {"hooks/guard.py": {"TRY203": 1, "TRY400": 1}}


def test_an_allowlisted_file_may_stay_but_not_grow(tmp_path, capsys):
    base = _recorded(tmp_path / "base", {"hooks/guard.py": SWALLOWED})
    head = _recorded(tmp_path / "head", {"hooks/guard.py": SWALLOWED + SWALLOWED.replace("guard", "other")})
    code, out = _grade(base, base, capsys)
    assert code == 0, out
    assert "1 files swallow exceptions, 1 on the base allowlist, 0 errors" in out
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "hooks/guard.py has 2 BLE001 hits; the base allowlist allows 1" in out


def test_a_shrunk_file_must_be_recorded(tmp_path, capsys):
    base = _recorded(tmp_path / "base", {"hooks/guard.py": SWALLOWED})
    head = _tree(tmp_path / "head", {"hooks/guard.py": NARROW})
    (head / swallowed_exceptions.ALLOWLIST).write_text((base / swallowed_exceptions.ALLOWLIST).read_text())
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "0 files swallow exceptions, 1 on the base allowlist, 1 errors" in out
    swallowed_exceptions.main(["--write", "--head", str(head)])
    assert _grade(base, head, capsys)[0] == 0
    assert swallowed_exceptions.load(head) == {}


def test_config_and_noqa_in_the_graded_tree_hide_nothing(tmp_path):
    tree = _tree(
        tmp_path,
        {
            "hooks/guard.py": SWALLOWED.replace("except Exception:", "except Exception:  # noqa: BLE001"),
            "pyproject.toml": (
                '[tool.ruff]\nexclude = ["hooks"]\n[tool.ruff.lint.per-file-ignores]\n"hooks/guard.py" = ["BLE001"]\n'
            ),
        },
    )
    assert swallowed_exceptions.measure(tree) == {"hooks/guard.py": {"BLE001": 1}}


def test_the_allowlist_is_written_sorted_one_space_indented(tmp_path):
    logged = "import logging\ndef log():\n    try:\n        return 1\n    except ValueError:\n        logging.error('lost')\n"
    tree = _recorded(tmp_path, {"hooks/b.py": logged + SWALLOWED, "hooks/a.py": SWALLOWED})
    assert (tree / swallowed_exceptions.ALLOWLIST).read_text() == (
        '{\n "hooks/a.py": {\n  "BLE001": 1\n },\n "hooks/b.py": {\n  "BLE001": 1,\n  "TRY400": 1\n }\n}\n'
    )


def test_a_base_without_an_allowlist_is_red(tmp_path, capsys):
    base = _tree(tmp_path / "base", {"hooks/guard.py": NARROW})
    head = _recorded(tmp_path / "head", {"hooks/guard.py": NARROW})
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "::error::the base has no tests/SWALLOWED_EXCEPTIONS_ALLOWLIST.json, so the head cannot be graded" in out


def test_a_tree_without_hooks_or_gates_cannot_be_graded(tmp_path, capsys):
    tree = _tree(tmp_path, {"scripts/other.py": SWALLOWED})
    with pytest.raises(size_limits.GradeError, match="^no tracked Python files under hooks/ or scripts/gates/ in "):
        swallowed_exceptions.measure(tree)
    assert swallowed_exceptions.main(["--write", "--head", str(tree)]) == 1
    assert "::error::no tracked Python files under hooks/ or scripts/gates/" in capsys.readouterr().out


def test_a_head_that_does_not_parse_cannot_be_graded(tmp_path, capsys):
    base = _recorded(tmp_path / "base", {"hooks/guard.py": NARROW})
    head = _tree(tmp_path / "head", {"hooks/guard.py": "def broken(:\n"})
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "::error::cannot grade " in out
    assert "hooks/guard.py: invalid-syntax " in out


def test_the_head_defaults_to_the_working_directory(tmp_path, monkeypatch):
    tree = _tree(tmp_path, {"hooks/guard.py": SWALLOWED})
    monkeypatch.chdir(tree)
    assert swallowed_exceptions.main(["--write"]) == 0
    assert json.loads((tree / swallowed_exceptions.ALLOWLIST).read_text()) == {"hooks/guard.py": {"BLE001": 1}}


def test_grading_without_a_base_is_refused(tmp_path, capsys):
    with pytest.raises(SystemExit) as raised:
        swallowed_exceptions.main(["--head", str(tmp_path)])
    assert raised.value.code == 2
    assert "grading needs --base" in capsys.readouterr().err


def test_a_failing_ruff_cannot_grade(tmp_path, monkeypatch):
    tree = _tree(tmp_path / "tree", {"hooks/guard.py": NARROW})
    fake = tmp_path / "fake_ruff.py"
    fake.write_text("import sys\nsys.stderr.write('boom\\n')\nsys.exit(3)\n")
    monkeypatch.setattr(swallowed_exceptions, "_RUFF", (sys.executable, str(fake)))
    with pytest.raises(size_limits.GradeError, match="^ruff exited 3: boom$"):
        swallowed_exceptions.measure(tree)


@pytest.mark.parametrize(("version", "shown"), [("0.0.1", "0.0.1"), ("", "none")])
def test_the_grader_refuses_another_ruff_version(tmp_path, capsys, monkeypatch, version, shown):
    monkeypatch.setattr(swallowed_exceptions, "_ruff_version", lambda: version)
    assert swallowed_exceptions.main(["--write", "--head", str(_tree(tmp_path, {"hooks/guard.py": NARROW}))]) == 1
    out = capsys.readouterr().out
    assert f"::error::The swallowed exception gate needs ruff {size_limits.RUFF_VERSION}, found {shown}.\n" == out


def _step():
    steps = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["lint"]["steps"]
    return next(step for step in steps if step.get("name") == "Hold swallowed exceptions in hooks and gates")


def test_the_gate_runs_in_lint_graded_by_the_protected_copy_against_the_base():
    step = _step()
    assert step["if"] == "${{ !cancelled() }}"
    assert step["run"] == (
        'if [[ -f "$RUNNER_TEMP/grader/scripts/swallowed_exceptions.py" ]]; then\n'
        '  cd "$RUNNER_TEMP/grader"\n'
        '  python -m scripts.swallowed_exceptions --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"\n'
        "else\n"
        '  python -m scripts.swallowed_exceptions --base "$GITHUB_WORKSPACE"\n'
        "fi\n"
    )


def test_a_dev_without_the_grader_holds_the_head_to_its_own_allowlist(tmp_path):
    head = _tree(tmp_path / "head", {"hooks/guard.py": SWALLOWED})
    (head / swallowed_exceptions.ALLOWLIST).write_text("{}\n")
    (tmp_path / "grader").mkdir()
    path = f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), GITHUB_WORKSPACE=str(head), PYTHONPATH=str(_ROOT), PATH=path)
    result = subprocess.run(["bash", "-e", "-c", _step()["run"]], env=env, cwd=head, capture_output=True, text=True)
    assert result.returncode == 1
    assert "hooks/guard.py has 1 BLE001 hits; the base allowlist allows 0" in result.stdout
