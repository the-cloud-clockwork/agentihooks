import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent


def _self_plant_changed(tmp_path: Path, edited: str) -> str:
    steps = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["lint-actions"]["steps"]
    [step] = [step for step in steps if step.get("id") == "self-plant"]
    repo = tmp_path / "repo"
    (repo / "scripts/ci_mutation").mkdir(parents=True)
    (repo / "scripts/ci_mutation/__init__.py").write_text("x = 1\n")
    (repo / "docs").mkdir()
    (repo / "docs/guide.md").write_text("old\n")
    (repo / ".github").mkdir()
    (repo / ".github/test-requirements.lock").write_text("old\n")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    subprocess.run([*git, "worktree", "add", "-q", "--detach", str(tmp_path / "base"), "HEAD"], check=True)
    (repo / edited).write_text("new\n")
    subprocess.run([*git, "commit", "-qam", "head"], check=True)
    (repo / "scripts/ci_mutation/__pycache__").mkdir()
    (repo / "scripts/ci_mutation/__pycache__/__init__.cpython-312.pyc").write_bytes(b"\0")
    output = tmp_path / "output"
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(output)}
    subprocess.run(["bash", "-e", "-c", step["run"]], cwd=repo, env=env, check=True, capture_output=True, text=True)
    return output.read_text()


def test_a_docs_only_diff_reports_no_mutation_gate_change(tmp_path: Path) -> None:
    assert _self_plant_changed(tmp_path, "docs/guide.md") == "changed=false\n"


def test_a_mutation_gate_source_change_still_plants(tmp_path: Path) -> None:
    assert _self_plant_changed(tmp_path, "scripts/ci_mutation/__init__.py") == "changed=true\n"


@pytest.mark.feature("tracked-test-lock-cache-keys")
def test_a_test_requirements_lock_change_still_plants(tmp_path: Path) -> None:
    assert _self_plant_changed(tmp_path, ".github/test-requirements.lock") == "changed=true\n"
