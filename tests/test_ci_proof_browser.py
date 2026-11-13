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


@pytest.mark.parametrize(
    ("baked", "offline_code", "expected"),
    [(True, 0, ["--offline"]), (True, 1, ["--offline", "--system"]), (False, 0, ["--system"])],
)
def test_proof_dependencies_install_offline_into_the_baked_venv(
    tmp_path: Path, baked: bool, offline_code: int, expected: list[str]
) -> None:
    """Build the editable wheel once into the runner's baked venv offline and fall back to a PyPI install."""
    install = next(step for step in _job()["steps"] if step.get("name") == "Install dependencies")
    venv = tmp_path / "venv"
    if baked:
        (venv / "bin").mkdir(parents=True)
        (venv / "bin/python").write_text('#!/usr/bin/env bash\ntouch "$3/agentihooks-0.editable-py3-none-any.whl"\n')
        (venv / "bin/python").chmod(0o755)
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "uv").write_text(
        '#!/usr/bin/env bash\necho "$*" >> "$CALLS"\n[[ "$*" != *--offline* ]] || exit "$OFFLINE_CODE"\n'
    )
    (tools / "uv").chmod(0o755)
    calls = tmp_path / "calls"
    github_path = tmp_path / "github_path"
    github_path.touch()
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    result = subprocess.run(
        ["bash", "-e", "-c", install["run"]],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "BAKED_VENV": str(venv),
            "WHEELHOUSE": install["env"]["WHEELHOUSE"],
            "CALLS": str(calls),
            "OFFLINE_CODE": str(offline_code),
            "GITHUB_PATH": str(github_path),
            "RUNNER_TEMP": str(runner_temp),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    lines = calls.read_text().splitlines()
    assert [next(flag for flag in ("--offline", "--system") if flag in line) for line in lines] == expected
    assert all("--constraint .github/test-requirements.lock" in line for line in lines)
    wheel = f"agentihooks[dev,all] @ file://{runner_temp}/editable/agentihooks-0.editable-py3-none-any.whl"
    assert [wheel in line if "--offline" in line else "-e .[dev,all]" in line for line in lines] == [True] * len(lines)
    hit = expected == ["--offline"]
    assert github_path.read_text() == (f"{venv}/bin\n" if hit else "")
    assert ("Baked venv hit" if hit else "Baked venv miss") in result.stdout
    assert install["env"]["BAKED_VENV"] == "/opt/agentihooks-venv"


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
    assert offline["if"] == "runner.environment == 'self-hosted'"
    hosted = next(step for step in steps if step.get("uses") == "./.github/actions/browser-cache")
    assert hosted["if"] == "inputs.browser && runner.environment != 'self-hosted'"
    isolated = next(step for step in steps if "proof-browser" in step.get("run", ""))
    assert isolated["if"] == "inputs.browser || runner.environment == 'self-hosted'"
    baked = tmp_path / "baked"
    baked.mkdir()
    calls = tmp_path / "calls"
    python = tmp_path / "python"
    python.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\n[[ "$*" != *install* ]]\necho launch >> "$CALLS"\nexit 47\n'
    )
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
