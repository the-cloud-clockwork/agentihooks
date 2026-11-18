import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.ci_for_substitution import ADVICE, RED_FIXTURE, findings, main

ROOT = Path(__file__).resolve().parents[1]
CLEAN_FIXTURE = "tests/fixtures/ci_for_substitution/clean.sh"
STEP = "Refuse a command substitution as a for loop word list"


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-f", *files], check=True)
    return root


def test_the_red_fixture_reports_every_refused_word_list_in_source_order() -> None:
    assert findings((ROOT / RED_FIXTURE).read_bytes()) == [4, 8, 12, 16, 20, 24, 29]


def test_the_clean_fixture_reports_nothing() -> None:
    assert findings((ROOT / CLEAN_FIXTURE).read_bytes()) == []


def test_loops_far_down_a_long_script_report_their_exact_lines() -> None:
    source = b"\n" * 299 + b"for name in $(ls); do :; done\n" * 100
    assert findings(source) == list(range(300, 400))


def test_tracked_shell_files_with_a_substitution_word_list_are_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(
        tmp_path,
        {
            "deploy/wait.sh": "#!/usr/bin/env bash\nfor _ in $(seq 3); do\n  sleep 1\ndone\n",
            "bin/tool": "#!/usr/bin/env bash\nset -e\nfor name in `ls`; do :; done\n",
            "notes.txt": "for name in $(ls); do :; done\n",
            "ok.sh": "#!/usr/bin/env bash\nfor _ in {1..3}; do :; done\n",
        },
    )
    (root / "loose.sh").write_text("for name in $(ls); do :; done\n")
    assert main(["--root", str(root)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "For loop word lists: 3 shell files, 2 problems",
        f"::error::bin/tool:3: {ADVICE}",
        f"::error::deploy/wait.sh:2: {ADVICE}",
    ]


def test_the_red_fixture_is_the_one_shell_file_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fixtures = {name: (ROOT / name).read_text() for name in (RED_FIXTURE, CLEAN_FIXTURE)}
    assert main(["--root", str(_repo(tmp_path, fixtures))]) == 0
    assert capsys.readouterr().out.splitlines() == ["For loop word lists: 1 shell files, 0 problems"]


def test_a_tree_without_a_tracked_shell_file_grades_nothing_and_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {"README.md": "for name in $(ls); do :; done\n"})
    assert main(["--root", str(root)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "For loop word lists: 0 shell files, 1 problems",
        "::error::No tracked shell file found; the check graded nothing",
    ]


def test_a_root_outside_git_fails_instead_of_grading_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    with pytest.raises(subprocess.CalledProcessError):
        main(["--root", str(tmp_path)])


def test_the_root_is_required() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([])
    assert exit_info.value.code == 2


def test_lint_grades_the_head_tree_with_the_protected_grader() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    step = next(step for step in workflow["jobs"]["lint"]["steps"] if step.get("name") == STEP)
    assert step["if"] == "${{ !cancelled() }}"
    assert 'cd "$RUNNER_TEMP/grader"' in step["run"]
    assert 'python -m scripts.ci_for_substitution --root "$GITHUB_WORKSPACE"' in step["run"]
