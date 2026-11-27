import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.ci_for_substitution import ADVICE, RED_FIXTURE, blank_expressions, findings, main, workflow_findings

ROOT = Path(__file__).resolve().parents[1]
CLEAN_FIXTURE = "tests/fixtures/ci_for_substitution/clean.sh"
RED_WORKFLOW = "tests/fixtures/ci_for_substitution/red.yml"
CLEAN_WORKFLOW = "tests/fixtures/ci_for_substitution/clean.yml"
WORKFLOW = (
    "on: push\njobs:\n  wait:\n    runs-on: ubuntu-latest\n    steps:\n      - run: for _ in {loop}; do :; done\n"
)
ACTION = "runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: |\n        for _ in {loop}; do :; done\n"
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


@pytest.mark.feature("ci-for-loop-word-list-workflow-run-blocks")
def test_the_red_workflow_fixture_reports_every_refused_loop_at_its_file_line() -> None:
    assert workflow_findings((ROOT / RED_WORKFLOW).read_text()) == [7, 11, 17, 24, 27, 30, 33, 43, 50]


@pytest.mark.feature("ci-for-loop-word-list-workflow-run-blocks")
def test_the_clean_workflow_fixture_reports_nothing() -> None:
    assert workflow_findings((ROOT / CLEAN_WORKFLOW).read_text()) == []


@pytest.mark.feature("ci-for-loop-word-list-workflow-run-blocks")
def test_a_run_block_ending_the_file_without_a_newline_keeps_its_last_line() -> None:
    assert workflow_findings(ACTION.format(loop="$(seq 3)").rstrip("\n")) == [6]


@pytest.mark.feature("ci-for-loop-word-list-workflow-run-blocks")
def test_github_expressions_become_underscores_that_keep_their_newlines() -> None:
    text = 'a=${{ inputs.a }} b="${{\n  inputs.b }}"\n'
    assert blank_expressions(text) == f'a={"_" * 15} b="{"_" * 3}\n{"_" * 13}"\n'


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
            ".github/workflows/ok.yml": WORKFLOW.format(loop="{1..3}"),
        },
    )
    (root / "loose.sh").write_text("for name in $(ls); do :; done\n")
    assert main(["--root", str(root)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "For loop word lists: 3 shell files, 1 workflow files, 2 problems",
        f"::error::bin/tool:3: {ADVICE}",
        f"::error::deploy/wait.sh:2: {ADVICE}",
    ]


@pytest.mark.feature("ci-for-loop-word-list-workflow-run-blocks")
def test_tracked_workflows_and_actions_with_a_substitution_word_list_are_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    refused = "$(seq 3)"
    root = _repo(
        tmp_path,
        {
            ".github/workflows/wait.yml": WORKFLOW.format(loop=refused),
            ".github/workflows/poll.yaml": WORKFLOW.format(loop=refused),
            ".github/workflows/red.yml": (ROOT / RED_WORKFLOW).read_text(),
            ".github/workflows/trusted.json": WORKFLOW.format(loop=refused),
            ".github/actions/tick/action.yml": ACTION.format(loop=refused),
            ".github/actions/old/action.yaml": ACTION.format(loop=refused),
            "deploy/wait.yml": WORKFLOW.format(loop=refused),
            "ok.sh": "#!/usr/bin/env bash\nfor _ in {1..3}; do :; done\n",
        },
    )
    assert main(["--root", str(root)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "For loop word lists: 1 shell files, 5 workflow files, 13 problems",
        f"::error::.github/actions/old/action.yaml:6: {ADVICE}",
        f"::error::.github/actions/tick/action.yml:6: {ADVICE}",
        f"::error::.github/workflows/poll.yaml:6: {ADVICE}",
        *(f"::error::.github/workflows/red.yml:{line}: {ADVICE}" for line in (7, 11, 17, 24, 27, 30, 33, 43, 50)),
        f"::error::.github/workflows/wait.yml:6: {ADVICE}",
    ]


def test_a_tree_of_the_fixtures_grades_the_clean_ones_and_skips_the_red_ones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixtures = {name: (ROOT / name).read_text() for name in (RED_FIXTURE, CLEAN_FIXTURE, RED_WORKFLOW)}
    fixtures[".github/workflows/clean.yml"] = (ROOT / CLEAN_WORKFLOW).read_text()
    assert main(["--root", str(_repo(tmp_path, fixtures))]) == 0
    assert capsys.readouterr().out.splitlines() == ["For loop word lists: 1 shell files, 1 workflow files, 0 problems"]


@pytest.mark.feature("ci-for-loop-word-list-workflow-run-blocks")
def test_a_tree_without_a_tracked_shell_or_workflow_file_grades_nothing_and_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {"README.md": "for name in $(ls); do :; done\n"})
    assert main(["--root", str(root)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "For loop word lists: 0 shell files, 0 workflow files, 2 problems",
        "::error::No tracked shell file found; the check graded no shell script",
        "::error::No tracked workflow file found; the check graded no run block",
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
    assert "if [[ -f" not in step["run"]
    assert 'python -m scripts.ci_for_substitution --root "$GITHUB_WORKSPACE"' in step["run"]


@pytest.mark.feature("ci-for-loop-word-list-workflow-run-blocks")
def test_no_workflow_or_action_in_the_repository_holds_a_refused_word_list() -> None:
    workflows = sorted(ROOT.glob(".github/workflows/*.y*ml"))
    actions = sorted(ROOT.glob(".github/actions/**/action.y*ml"))
    assert workflows and actions
    found = {path.relative_to(ROOT).as_posix(): workflow_findings(path.read_text()) for path in workflows + actions}
    assert {name: lines for name, lines in found.items() if lines} == {}
