"""The lint job's artifact sanity step runs the protected grader copy of the checker on the head page folder."""

import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CHECKER = Path("scripts") / "swarm_ledger" / "artifact_sanity.py"


def _lint_steps() -> list[dict[str, str]]:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "test.yml").read_text())["jobs"]["lint"]["steps"]


def _named(steps: list[dict[str, str]], name: str) -> dict[str, str]:
    return next(step for step in steps if step.get("name") == name)


def _run_step(tmp_path: Path, grader_option: str) -> str:
    grader, head, tools = tmp_path / "temp" / "grader", tmp_path / "head", tmp_path / "bin"
    for root, option in ((grader, grader_option), (head, '"--pages"')):
        (root / CHECKER).parent.mkdir(parents=True)
        (root / CHECKER).write_text(f"parser.add_argument({option}, type=Path)\n")
    fixtures = head / "tests" / "fixtures" / "artifacts"
    fixtures.mkdir(parents=True)
    (fixtures / "notes.md").write_text("# Notes\n")
    tools.mkdir()
    (tools / "python").write_text('#!/usr/bin/env bash\necho "$PWD $*" >> "$CALLS"\n')
    (tools / "python").chmod(0o755)
    env = {
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path / "temp"),
        "GITHUB_WORKSPACE": str(head),
        "CALLS": str(tmp_path / "calls"),
    }
    script = _named(_lint_steps(), "Artifact sanity checks")["run"]
    subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script], cwd=head, env=env, check=True)
    return (tmp_path / "calls").read_text()


def test_the_grader_is_checked_out_before_the_artifact_checks() -> None:
    steps = _lint_steps()
    grader = _named(steps, "Check out the protected grader")
    assert steps.index(grader) < steps.index(_named(steps, "Artifact sanity checks"))


def test_the_grader_copy_of_the_checker_grades_the_head_pages_and_fixtures(tmp_path: Path) -> None:
    head = tmp_path / "head"
    pages, notes = head / "scripts" / "swarm_ledger", head / "tests" / "fixtures" / "artifacts" / "notes.md"
    assert _run_step(tmp_path, '"--pages"') == (
        f"{tmp_path / 'temp' / 'grader'} -m scripts.swarm_ledger.artifact_sanity --pages {pages} {notes}\n"
    )


def test_a_grader_without_the_pages_option_runs_the_head_checker(tmp_path: Path) -> None:
    assert _run_step(tmp_path, '"files"') == (
        f"{tmp_path / 'head'} -m scripts.swarm_ledger.artifact_sanity tests/fixtures/artifacts/notes.md\n"
    )


def test_the_step_detects_the_option_the_checker_really_declares() -> None:
    assert '"--pages"' in (ROOT / CHECKER).read_text()
