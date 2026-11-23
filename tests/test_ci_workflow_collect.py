import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent


def _workflow() -> dict:
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())


@pytest.mark.parametrize("exit_code", [0, 3], ids=["success", "failure"])
@pytest.mark.feature("lab-static-jobs-hold-lab-steps")
def test_the_collection_step_propagates_its_exit_code(tmp_path: Path, exit_code: int) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    python = binary / "python"
    python.write_text(f"#!/usr/bin/env bash\nexit {exit_code}\n")
    python.chmod(0o755)
    step = next(
        step for step in _workflow()["jobs"]["collect"]["steps"] if step.get("name") == "Collect the head's tests"
    )
    outcome = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(
            os.environ,
            PATH=f"{binary}:{os.environ['PATH']}",
            COLLECTED=str(tmp_path / "collected.json"),
        ),
        capture_output=True,
        text=True,
    )
    assert outcome.returncode == exit_code


@pytest.mark.parametrize(
    ("mode", "exit_code", "expected"),
    [(True, 0, 0), (True, 1, 1), (False, 0, 1)],
    ids=["approved", "refused", "base without the mode"],
)
@pytest.mark.feature("count-floor-retirement-needs-code-owner-approval")
def test_the_approval_hold_runs_the_base_floor_and_ends_with_its_exit_code(
    tmp_path: Path, mode: bool, exit_code: int, expected: int
) -> None:
    base = tmp_path / "base"
    (base / "tests").mkdir(parents=True)
    (base / "tests" / "count_floor.py").write_text('parser.add_argument("--approvals")\n' if mode else "")
    calls = tmp_path / "calls"
    binary = tmp_path / "bin"
    binary.mkdir()
    python = binary / "python"
    python.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$PWD" "$@" > "{calls}"\nexit {exit_code}\n')
    python.chmod(0o755)
    step = next(
        step
        for step in _workflow()["jobs"]["collect"]["steps"]
        if step.get("name") == "Hold each new retirement to a code owner's approval"
    )
    outcome = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(
            os.environ,
            PATH=f"{binary}:{os.environ['PATH']}",
            RUNNER_TEMP=str(tmp_path),
            GITHUB_WORKSPACE=str(tmp_path / "head"),
        ),
        capture_output=True,
        text=True,
    )
    assert outcome.returncode == expected, outcome.stdout + outcome.stderr
    if mode:
        floor = ["-m", "tests.count_floor", "--approvals", "--base", str(base), "--head", str(tmp_path / "head")]
        assert calls.read_text().splitlines() == [str(base), *floor]
    else:
        assert not calls.exists()
        assert "::error::The base carries no count floor approvals mode" in outcome.stdout
