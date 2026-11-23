"""The lint-actions job's artifact sanity step runs the protected grader copy of the checker on the head page folder."""

import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CHECKER = Path("scripts") / "swarm_ledger" / "artifact_sanity.py"


NO_PAGES = (
    "::error::The protected grader carries no artifact sanity checker with the pages option, "
    "so nothing trusted can grade the head viewer page and assets.\n"
)


def _lint_actions_steps() -> list[dict[str, str]]:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "test.yml").read_text())["jobs"]["lint-actions"]["steps"]


def _named(steps: list[dict[str, str]], name: str) -> dict[str, str]:
    return next(step for step in steps if step.get("name") == name)


def _checker(name: str, option: str, code: int) -> str:
    return (
        "import os, sys\n"
        f"OPTION = {option}\n"
        "with open(os.environ['CALLS'], 'a') as calls:\n"
        f"    calls.write(' '.join(['{name}', os.getcwd(), *sys.argv[1:]]) + '\\n')\n"
        f"sys.exit({code})\n"
    )


def _run_step(tmp_path: Path, grader_option: str | None) -> tuple[int, str]:
    grader, head, tools = tmp_path / "temp" / "grader", tmp_path / "head", tmp_path / "bin"
    grader.mkdir(parents=True)
    checkers = [(head, _checker("planted", '"--pages"', 0))]
    if grader_option is not None:
        checkers.append((grader, _checker("grader", grader_option, 1)))
    for root, source in checkers:
        (root / CHECKER).parent.mkdir(parents=True)
        (root / CHECKER).write_text(source)
        (root / "scripts" / "__init__.py").write_text("")
        (root / CHECKER.parent / "__init__.py").write_text("")
    fixtures = head / "tests" / "fixtures" / "artifacts"
    fixtures.mkdir(parents=True)
    (fixtures / "notes.md").write_text("# Notes\n")
    tools.mkdir()
    (tools / "python").symlink_to(sys.executable)
    env = {
        **os.environ,
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path / "temp"),
        "GITHUB_WORKSPACE": str(head),
        "CALLS": str(tmp_path / "calls"),
    }
    script = _named(_lint_actions_steps(), "Artifact sanity checks")["run"]
    command = ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script]
    done = subprocess.run(command, cwd=head, env=env, capture_output=True, text=True)
    calls = tmp_path / "calls"
    return done.returncode, (calls.read_text() if calls.exists() else "") + done.stdout


def test_the_grader_is_checked_out_before_the_artifact_checks() -> None:
    steps = _lint_actions_steps()
    grader = _named(steps, "Check out the protected grader")
    assert steps.index(grader) < steps.index(_named(steps, "Artifact sanity checks"))


def test_a_planted_head_checker_never_runs_once_the_grader_carries_the_pages_option(tmp_path: Path) -> None:
    head = tmp_path / "head"
    pages, notes = head / "scripts" / "swarm_ledger", head / "tests" / "fixtures" / "artifacts" / "notes.md"
    assert _run_step(tmp_path, '"--pages"') == (1, f"grader {tmp_path / 'temp' / 'grader'} --pages {pages} {notes}\n")


def test_a_grader_without_the_pages_option_turns_the_step_red(tmp_path: Path) -> None:
    assert _run_step(tmp_path, '"files"') == (1, NO_PAGES)


def test_a_grader_without_the_checker_turns_the_step_red(tmp_path: Path) -> None:
    assert _run_step(tmp_path, None) == (1, NO_PAGES)


def test_the_step_detects_the_option_the_checker_really_declares() -> None:
    assert '"--pages"' in (ROOT / CHECKER).read_text()
