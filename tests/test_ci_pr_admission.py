import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.size_limits import RUFF_VERSION

pytestmark = pytest.mark.unit


@pytest.fixture
def jobs():
    root = Path(__file__).resolve().parents[1]
    return yaml.safe_load((root / ".github/workflows/test.yml").read_text())["jobs"]


@pytest.mark.parametrize("command", ["ruff check", "ruff format --check", "scripts.size_limits"])
def test_pull_request_selection_waits_for_each_cheap_check(jobs, command):
    steps = jobs["select"]["steps"]
    selection = next(i for i, step in enumerate(steps) if step.get("id") == "select")
    gates = [step for step in steps[:selection] if command in step.get("run", "")]
    assert len(gates) == 1, f"Selection must wait for {command}"
    assert gates[0].get("if") == "github.event_name == 'pull_request'"
    assert not gates[0].get("continue-on-error", False)
    assert "|| true" not in gates[0]["run"]
    assert "if" not in steps[selection]


def test_both_unit_routes_wait_for_the_checked_selection(jobs):
    for name in ("split", "unit", "unit-selected"):
        assert "select" in jobs[name]["needs"]
        assert "needs.select.outputs.scope" in jobs[name]["if"]
    assert jobs["select"]["needs"] == ["reuse", "admission"]


def test_size_admission_uses_the_protected_grader_and_actual_base(jobs):
    steps = jobs["select"]["steps"]
    size = next(step for step in steps if "scripts.size_limits" in step.get("run", ""))
    assert 'cd "$RUNNER_TEMP/grader"' in size["run"]
    assert '--base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"' in size["run"]
    base = next(step for step in steps if '"$RUNNER_TEMP/base"' in step.get("run", "") and step is not size)
    assert base["env"]["BASE"] == "${{ github.event.pull_request.base.sha }}"
    assert steps.index(base) < steps.index(size)


def test_pull_request_selection_clones_the_repository_once(jobs):
    steps = jobs["select"]["steps"]
    clones = [
        step
        for step in steps
        if step.get("uses") == "actions/checkout@v4" and "pull_request" in step.get("if", "pull_request")
    ]
    assert [step["with"]["path"] for step in clones] == ["provenance-grader"]
    head = next(step for step in steps if step.get("name") == "Check out the head from the protected checkout")
    assert head["if"] == "github.event_name == 'pull_request'"
    assert steps.index(clones[0]) < steps.index(head)
    assert 'worktree add --detach "$GITHUB_WORKSPACE" "$GITHUB_SHA"' in head["run"]


OTHER_RUFF = "0.0.1"
RUFF_PYTHON_STUB = (
    "#!/usr/bin/env bash\n"
    'if [[ "$*" == "-I -m ruff --version" ]]; then\n'
    '  [[ -f "$STUBS/version" ]] || exit 1\n'
    '  exec cat "$STUBS/version"\n'
    "fi\n"
    'echo "$*" >> "$STUBS/calls"\n'
    'if [[ "$*" == *ruff==* ]]; then echo "ruff $PIP_GIVES" > "$STUBS/version"; fi\n'
)


def _install_ruff(
    jobs: dict[str, Any], tmp_path: Path, installed: str, pip_gives: str = RUFF_VERSION
) -> tuple[int, list[str], str]:
    step = next(step for step in jobs["select"]["steps"] if step.get("name") == "Install admission ruff")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python"
    python.write_text(RUFF_PYTHON_STUB)
    python.chmod(0o755)
    if installed:
        (tmp_path / "version").write_text(f"ruff {installed}\n")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "STUBS": str(tmp_path), "PIP_GIVES": pip_gives}
    done = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], env=env, capture_output=True, text=True)
    calls = tmp_path / "calls"
    return done.returncode, calls.read_text().splitlines() if calls.exists() else [], done.stdout


def test_selection_takes_ruff_from_the_runner_image_when_it_carries_the_pin(
    jobs: dict[str, Any], tmp_path: Path
) -> None:
    code, calls, out = _install_ruff(jobs, tmp_path, RUFF_VERSION)
    assert (code, calls) == (0, [])
    assert f"ruff {RUFF_VERSION} comes from the runner image" in out


@pytest.mark.parametrize("installed", ["", OTHER_RUFF])
def test_selection_installs_the_pinned_ruff_without_a_matching_runner_copy(
    jobs: dict[str, Any], tmp_path: Path, installed: str
) -> None:
    code, calls, _ = _install_ruff(jobs, tmp_path, installed)
    assert (code, calls) == (0, [f"-m pip install --constraint .github/ci-tools/lint-tools.lock ruff=={RUFF_VERSION}"])


def test_selection_fails_when_its_ruff_differs_from_the_pinned_lint_version(
    jobs: dict[str, Any], tmp_path: Path
) -> None:
    code, calls, _ = _install_ruff(jobs, tmp_path, "", pip_gives=OTHER_RUFF)
    assert (code, calls) == (1, [f"-m pip install --constraint .github/ci-tools/lint-tools.lock ruff=={RUFF_VERSION}"])


def test_the_lock_the_runner_image_smoke_reads_pins_the_lint_ruff() -> None:
    lock = Path(__file__).resolve().parents[1] / ".github/test-requirements.lock"
    assert f"ruff=={RUFF_VERSION}" in lock.read_text().splitlines()
