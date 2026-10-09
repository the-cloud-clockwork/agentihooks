import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from scripts import size_limits

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit

EIGHT_PARAMETERS = "def planted(a, b, c, d, e, f, g, h):\n    return a\n"
SEVEN_PARAMETERS = "def planted(a, b, c, d, e, f, g):\n    return a\n"


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
    assert size_limits.main(["--write", "--head", str(tree)]) == 0
    return tree


def _grade(base: Path, head: Path, capsys) -> tuple[int, str]:
    code = size_limits.main(["--base", str(base), "--head", str(head)])
    return code, capsys.readouterr().out


def test_the_grader_pins_the_ruff_the_tests_install():
    dev = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]["dev"]
    assert f"ruff=={size_limits.RUFF_VERSION}" in dev


_INSTALL = re.compile(r"\b(?:pip3? install|pipx (?:install|run)|uv tool (?:install|run)|uvx)\b[^\n;&|]*")
_RUFF_SPEC = re.compile(r"(?<![\w./-])['\"]?(ruff(?![\w-])[^\s'\"]*)")


def _ruff_installs(steps: list) -> list[str]:
    specs = []
    for step in steps:
        if "ruff" in (step.get("uses") or ""):
            specs.append(step["uses"])
        run = (step.get("run") or "").replace("\\\n", " ")
        for command in _INSTALL.findall(run):
            specs += _RUFF_SPEC.findall(command)
    return specs


def test_every_workflow_installs_the_pinned_ruff():
    pinned = f"ruff=={size_limits.RUFF_VERSION}"
    installs = {}
    for path in sorted((_ROOT / ".github/workflows").glob("*.y*ml")):
        for name, job in (yaml.safe_load(path.read_text()).get("jobs") or {}).items():
            installs[f"{path.name}:{name}"] = _ruff_installs(job.get("steps") or [])
    for path in sorted((_ROOT / ".github/actions").glob("*/action.y*ml")):
        installs[path.parent.name] = _ruff_installs(yaml.safe_load(path.read_text()).get("runs", {}).get("steps") or [])
    assert installs["test.yml:lint"] == installs["test.yml:size"] == [pinned]
    offenders = {where: specs for where, specs in installs.items() if set(specs) - {pinned}}
    assert not offenders


def test_a_planted_eight_parameter_function_is_red(tmp_path, capsys):
    base = _recorded(tmp_path / "base", {"pkg/mod.py": SEVEN_PARAMETERS})
    head = _recorded(tmp_path / "head", {"pkg/mod.py": EIGHT_PARAMETERS})
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "1 units over a limit, 0 on the base allowlist, 1 errors" in out
    assert "pkg/mod.py::planted breaks PLR0913 at 8; it is not on the base allowlist" in out


@pytest.mark.parametrize("folder", ["dist", ".venv", "venv", "site-packages", "node_modules", "ignored"])
def test_offenders_in_excluded_or_ignored_folders_are_graded(tmp_path, folder):
    tree = _tree(tmp_path, {f"{folder}/mod.py": EIGHT_PARAMETERS, ".gitignore": "ignored/\n"})
    assert size_limits.measure(tree) == {f"{folder}/mod.py::planted": {"PLR0913": 8}}


def test_a_python_script_without_an_extension_is_graded(tmp_path):
    tree = _tree(
        tmp_path,
        {
            "bin/tool": f"#!/usr/bin/env python3\n{EIGHT_PARAMETERS}",
            "bin/job": f"#!/usr/bin/env pypy3\n{EIGHT_PARAMETERS}",
            "gui.pyw": EIGHT_PARAMETERS,
            "stub.pyi": "x: int\n" * (size_limits.FILE_LINES + 1),
            "bin/run": "#!/bin/sh\n",
            "notes": "python notes\n",
            "bin/far": f"#!{' ' * 249}python\n{EIGHT_PARAMETERS}",
        },
    )
    assert size_limits.measure(tree) == {
        "bin/tool::planted": {"PLR0913": 8},
        "bin/job::planted": {"PLR0913": 8},
        "gui.pyw::planted": {"PLR0913": 8},
        "stub.pyi": {"file-lines": size_limits.FILE_LINES + 1},
    }


def test_a_tracked_file_missing_from_the_tree_is_red(tmp_path, capsys):
    base = _recorded(tmp_path / "base", {"mod.py": SEVEN_PARAMETERS})
    head = _recorded(tmp_path / "head", {"mod.py": SEVEN_PARAMETERS, "gone.py": SEVEN_PARAMETERS})
    (head / "gone.py").unlink()
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "gone.py" in out


def test_config_and_noqa_in_the_graded_tree_hide_nothing(tmp_path):
    tree = _tree(
        tmp_path,
        {
            "mod.py": "def planted(a, b, c, d, e, f, g, h):  # noqa: PLR0913\n    return a\n",
            "pyproject.toml": (
                '[tool.ruff]\nexclude = ["mod.py"]\n[tool.ruff.lint.pylint]\nmax-args = 20\n'
                '[tool.ruff.lint.per-file-ignores]\n"mod.py" = ["PLR0913"]\n'
            ),
        },
    )
    assert size_limits.measure(tree) == {"mod.py::planted": {"PLR0913": 8}}


def test_a_ruff_module_in_the_graded_tree_does_not_shadow_ruff(tmp_path):
    tree = _tree(tmp_path, {"mod.py": EIGHT_PARAMETERS, "ruff.py": "print('[]')\n"})
    assert size_limits.measure(tree) == {"mod.py::planted": {"PLR0913": 8}}


def test_a_second_definition_of_an_allowlisted_name_is_a_new_unit(tmp_path, capsys):
    base = _recorded(tmp_path / "base", {"mod.py": EIGHT_PARAMETERS})
    twice = (
        "if True:\n    def planted(a, b, c, d, e, f, g, h): ...\nelse:\n    def planted(a, b, c, d, e, f, g, h): ...\n"
    )
    head = _recorded(tmp_path / "head", {"mod.py": twice})
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "mod.py::planted#2 breaks PLR0913 at 8" in out


def test_an_allowlisted_unit_may_stay_but_not_grow(tmp_path, capsys):
    base = _recorded(tmp_path / "base", {"mod.py": EIGHT_PARAMETERS})
    head = _recorded(tmp_path / "head", {"mod.py": EIGHT_PARAMETERS.replace("h)", "h, i)")})
    code, out = _grade(base, base, capsys)
    assert code == 0, out
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "mod.py::planted grew on PLR0913 from 8 to 9" in out


def test_a_shrunk_unit_must_be_recorded(tmp_path, capsys):
    base = _recorded(tmp_path / "base", {"mod.py": EIGHT_PARAMETERS})
    head = _tree(tmp_path / "head", {"mod.py": SEVEN_PARAMETERS})
    (head / size_limits.ALLOWLIST).write_text((base / size_limits.ALLOWLIST).read_text())
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "differs from the head's measurement" in out
    size_limits.main(["--write", "--head", str(head)])
    assert _grade(base, head, capsys)[0] == 0
    assert size_limits.load(head) == {}


def test_a_pull_request_cannot_allowlist_its_own_offender(tmp_path, capsys):
    base = _recorded(tmp_path / "base", {"mod.py": SEVEN_PARAMETERS})
    head = _recorded(tmp_path / "head", {"mod.py": EIGHT_PARAMETERS})
    assert size_limits.load(head) == {"mod.py::planted": {"PLR0913": 8}}
    assert _grade(base, head, capsys)[0] == 1


def test_a_base_without_an_allowlist_is_red(tmp_path, capsys):
    base = _tree(tmp_path / "base", {"mod.py": SEVEN_PARAMETERS})
    head = _recorded(tmp_path / "head", {"mod.py": EIGHT_PARAMETERS})
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "the base has no tests/SIZE_ALLOWLIST.json" in out


def _function(name: str, lines: int) -> str:
    return f"def {name}():\n" + "".join(f"    x{i} = {i}\n" for i in range(lines - 1))


def test_function_and_file_lengths_over_the_limit_are_measured(tmp_path):
    tree = _tree(
        tmp_path,
        {
            "long.py": "class Holder:\n"
            + "".join(f"    {line}\n" for line in _function("method", size_limits.FUNCTION_LINES + 1).splitlines()),
            "fits.py": _function("method", size_limits.FUNCTION_LINES),
            "big.py": "x = 1\n" * (size_limits.FILE_LINES + 1),
            "full.py": "x = 1\n" * size_limits.FILE_LINES,
        },
    )
    assert size_limits.measure(tree) == {
        "long.py::Holder.method": {"function-lines": size_limits.FUNCTION_LINES + 1},
        "big.py": {"file-lines": size_limits.FILE_LINES + 1},
    }


def test_the_allowlist_is_written_sorted_one_space_indented(tmp_path):
    tree = _recorded(tmp_path, {"big.py": "x = 1\n" * (size_limits.FILE_LINES + 1), "a.py": EIGHT_PARAMETERS})
    assert (tree / size_limits.ALLOWLIST).read_text() == (
        '{\n "a.py::planted": {\n  "PLR0913": 8\n },\n "big.py": {\n  "file-lines": 3001\n }\n}\n'
    )


def test_the_head_defaults_to_the_working_directory(tmp_path, monkeypatch):
    tree = _tree(tmp_path, {"mod.py": EIGHT_PARAMETERS})
    monkeypatch.chdir(tree)
    assert size_limits.main(["--write"]) == 0
    assert size_limits.load(tree) == {"mod.py::planted": {"PLR0913": 8}}


def test_grading_without_a_base_is_refused(tmp_path, capsys):
    with pytest.raises(SystemExit):
        size_limits.main(["--head", str(tmp_path)])
    assert capsys.readouterr().err.endswith(": error: grading needs --base\n")


def test_the_retired_bootstrap_flag_is_refused(tmp_path, capsys):
    with pytest.raises(SystemExit):
        size_limits.main(["--bootstrap", "--head", str(tmp_path)])
    assert capsys.readouterr().err.endswith(": error: unrecognized arguments: --bootstrap\n")


def test_a_tree_outside_git_or_without_python_cannot_be_graded(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(size_limits.GradeError, match=f"^git ls-files failed in {re.escape(str(plain))}: fatal"):
        size_limits.files(plain)
    empty = _tree(tmp_path / "empty", {"README.md": "text\n"})
    with pytest.raises(size_limits.GradeError, match=f"^no tracked Python files under {re.escape(str(empty))}$"):
        size_limits.files(empty)


def _fake_ruff(tmp_path: Path, monkeypatch, script: str) -> None:
    fake = tmp_path / "fake_ruff.py"
    fake.write_text(script)
    monkeypatch.setattr(size_limits, "_RUFF", (size_limits.sys.executable, str(fake)))


def test_a_failing_ruff_cannot_grade(tmp_path, monkeypatch):
    tree = _tree(tmp_path / "tree", {"mod.py": SEVEN_PARAMETERS})
    _fake_ruff(tmp_path, monkeypatch, "import sys\nsys.stderr.write('boom\\n')\nsys.exit(3)\n")
    with pytest.raises(size_limits.GradeError, match="^ruff exited 3: boom$"):
        size_limits.measure(tree)


@pytest.mark.parametrize(
    ("name", "code", "row", "message"),
    [
        ("mod.py", "E999", 1, "unknown (8 > 7)"),
        ("mod.py", "PLR0913", 1, "no measured value"),
        ("mod.py", "PLR0913", 9, "Too many arguments (8 > 7)"),
        ("other.py", "PLR0913", 1, "Too many arguments (8 > 7)"),
    ],
)
def test_a_ruff_hit_off_a_known_function_cannot_grade(tmp_path, monkeypatch, name, code, row, message):
    tree = _tree(tmp_path / "tree", {"mod.py": SEVEN_PARAMETERS}).resolve()
    hit = {"code": code, "filename": str(tree / name), "location": {"row": row}, "message": message}
    _fake_ruff(tmp_path, monkeypatch, f"import json\nprint(json.dumps([{hit!r}]))\n")
    with pytest.raises(size_limits.GradeError) as raised:
        size_limits.measure(tree)
    assert str(raised.value) == f"cannot grade {tree / name}:{row}: {code} {message}"


def test_the_grader_reads_the_installed_ruff_version():
    assert size_limits._ruff_version() == size_limits.RUFF_VERSION


@pytest.mark.parametrize(("version", "shown"), [("0.0.1", "0.0.1"), ("", "none")])
def test_the_grader_refuses_another_ruff_version(tmp_path, capsys, monkeypatch, version, shown):
    monkeypatch.setattr(size_limits, "_ruff_version", lambda: version)
    assert size_limits.main(["--write", "--head", str(_tree(tmp_path, {"mod.py": SEVEN_PARAMETERS}))]) == 1
    assert f"needs ruff {size_limits.RUFF_VERSION}, found {shown}, so it cannot grade" in capsys.readouterr().out


def test_size_runs_beside_unit_graded_by_the_base_with_the_pinned_ruff():
    jobs = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    job = jobs["size"]
    assert "needs" not in job
    assert "size" in jobs["gate-required"]["needs"]
    install, base, grader, grade = job["steps"][-4:]
    assert install["run"] == f"python -m pip install ruff=={size_limits.RUFF_VERSION}"
    assert base["run"] == 'git worktree add --detach "$RUNNER_TEMP/base" "$BASE"'
    assert grader["run"] == (
        'git fetch --no-tags origin dev\ngit worktree add --detach "$RUNNER_TEMP/grader" FETCH_HEAD\n'
    )
    assert grade["run"] == (
        'if [[ -f "$RUNNER_TEMP/grader/scripts/size_limits.py" ]]; then\n'
        '  cd "$RUNNER_TEMP/grader"\n'
        '  python -m scripts.size_limits --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"\n'
        "else\n"
        '  echo "::error::dev carries no scripts/size_limits.py, so nothing trusted can grade."\n'
        "  exit 1\n"
        "fi\n"
    )


def test_size_refuses_a_dev_without_its_grader(tmp_path):
    grade = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["size"]["steps"][-1]
    (tmp_path / "grader").mkdir()
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), GITHUB_WORKSPACE=str(tmp_path))
    result = subprocess.run(["bash", "-e", "-c", grade["run"]], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "dev carries no scripts/size_limits.py, so nothing trusted can grade." in result.stdout
