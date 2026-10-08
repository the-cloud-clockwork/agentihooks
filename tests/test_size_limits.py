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


def test_a_planted_eight_parameter_function_is_red(tmp_path, capsys):
    base = _recorded(tmp_path / "base", {"pkg/mod.py": SEVEN_PARAMETERS})
    head = _recorded(tmp_path / "head", {"pkg/mod.py": EIGHT_PARAMETERS})
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "pkg/mod.py::planted breaks PLR0913 at 8; it is not on the base allowlist" in out


@pytest.mark.parametrize("folder", ["dist", ".venv", "venv", "site-packages", "node_modules", "ignored"])
def test_offenders_in_excluded_or_ignored_folders_are_graded(tmp_path, folder):
    tree = _tree(tmp_path, {f"{folder}/mod.py": EIGHT_PARAMETERS, ".gitignore": "ignored/\n"})
    assert size_limits.measure(tree) == {f"{folder}/mod.py::planted": {"PLR0913": 8}}


def test_config_and_noqa_in_the_graded_tree_hide_nothing(tmp_path):
    tree = _tree(
        tmp_path,
        {
            "mod.py": "def planted(a, b, c, d, e, f, g, h):  # noqa: PLR0913\n    return a\n",
            "pyproject.toml": '[tool.ruff]\nexclude = ["mod.py"]\n[tool.ruff.lint.pylint]\nmax-args = 20\n',
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


def test_a_base_without_an_allowlist_is_red_unless_bootstrapping(tmp_path, capsys):
    base = _tree(tmp_path / "base", {"mod.py": SEVEN_PARAMETERS})
    head = _recorded(tmp_path / "head", {"mod.py": EIGHT_PARAMETERS})
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "the base has no tests/SIZE_ALLOWLIST.json" in out
    assert size_limits.main(["--bootstrap", "--head", str(head)]) == 0


def test_function_and_file_lengths_are_measured(tmp_path):
    statements = "".join(f"        x{i} = {i}\n" for i in range(size_limits.FUNCTION_LINES))
    tree = _tree(
        tmp_path,
        {
            "long.py": f"class Holder:\n    def method(self):\n{statements}",
            "big.py": "x = 1\n" * (size_limits.FILE_LINES + 1),
        },
    )
    measured = size_limits.measure(tree)
    assert measured["long.py::Holder.method"]["function-lines"] == size_limits.FUNCTION_LINES + 1
    assert measured["big.py"] == {"file-lines": size_limits.FILE_LINES + 1}


def test_the_grader_refuses_another_ruff_version(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(size_limits, "_ruff_version", lambda: "0.0.1")
    assert size_limits.main(["--write", "--head", str(_tree(tmp_path, {"mod.py": SEVEN_PARAMETERS}))]) == 1
    assert f"needs ruff {size_limits.RUFF_VERSION}, found 0.0.1" in capsys.readouterr().out


def test_size_runs_beside_unit_graded_by_the_base_with_the_pinned_ruff():
    jobs = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    job = jobs["size"]
    assert "needs" not in job
    assert "size" in jobs["gate-required"]["needs"]
    install, base, grade = job["steps"][-3:]
    assert install["run"] == f"python -m pip install ruff=={size_limits.RUFF_VERSION}"
    assert base["run"] == 'git worktree add --detach "$RUNNER_TEMP/base" "$BASE"'
    assert grade["run"] == (
        'if [[ -f "$RUNNER_TEMP/base/scripts/size_limits.py" ]]; then\n'
        '  cd "$RUNNER_TEMP/base"\n'
        '  python -m scripts.size_limits --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"\n'
        "else\n"
        '  python -m scripts.size_limits --bootstrap --head "$GITHUB_WORKSPACE"\n'
        "fi\n"
    )
