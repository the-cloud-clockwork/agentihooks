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
