import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _job():
    return yaml.safe_load((ROOT / ".github/workflows/proofs.yml").read_text())["jobs"]["tests"]


def test_proof_dependencies_use_the_baked_playwright_constraint():
    job = _job()
    assert job["env"]["UV_CONSTRAINT"] == "${{ github.workspace }}/.github/test-constraints.txt"
    install = next(step for step in job["steps"] if step.get("name") == "Install dependencies")
    assert "uv pip install --system" in install["run"]


@pytest.mark.parametrize("isolated", [False, True])
def test_proof_home_cannot_import_a_shadow_playwright(tmp_path, isolated):
    job = _job()
    assert job["env"]["PYTHONNOUSERSITE"] == "1"
    userbase = tmp_path / "user"
    package = (
        userbase / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages" / "playwright"
    )
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('raise RuntimeError("shadow Playwright imported")\n')
    home = tmp_path / ("isolated" if isolated else "runner")
    home.mkdir()
    result = subprocess.run(
        [sys.executable, "-c", "import playwright; print(playwright.__file__)"],
        env={
            **os.environ,
            "HOME": str(home),
            "PYTHONUSERBASE": str(userbase),
            "PYTHONNOUSERSITE": job["env"]["PYTHONNOUSERSITE"],
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert str(userbase) not in result.stdout


def test_proof_baked_browser_drift_fails_without_a_download(tmp_path):
    steps = _job()["steps"]
    offline = next(step for step in steps if "use-image.sh" in step.get("run", ""))
    assert offline["if"] == "inputs.browser && runner.environment == 'self-hosted'"
    hosted = next(step for step in steps if step.get("uses") == "./.github/actions/browser-cache")
    assert hosted["if"] == "inputs.browser && runner.environment != 'self-hosted'"
    baked = tmp_path / "baked"
    baked.mkdir()
    calls = tmp_path / "calls"
    python = tmp_path / "python"
    python.write_text('#!/usr/bin/env bash\nset -euo pipefail\necho "$*" >> "$CALLS"\nexit 47\n')
    python.chmod(0o755)
    target = tmp_path / ".playwright"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", offline["run"]],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "ARC_PLAYWRIGHT_BROWSERS_PATH": str(baked),
            "PLAYWRIGHT_BROWSERS_PATH": str(target),
            "CALLS": str(calls),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 47, result.stderr
    assert target.resolve() == baked
    assert len(calls.read_text().splitlines()) == 1
    assert "install" not in calls.read_text()
