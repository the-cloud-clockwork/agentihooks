import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent


def _pytest_command() -> str:
    for line in (_ROOT / ".github/workflows/test.yml").read_text().splitlines():
        if "pytest" in line and line.strip().startswith("run:"):
            return line
    raise AssertionError("no pytest step in test.yml")


def test_dev_extra_declares_xdist():
    dev = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]["dev"]
    assert any(dep.startswith("pytest-xdist") for dep in dev)


def test_workflow_runs_whole_suite_in_parallel_with_coverage():
    command = _pytest_command()
    assert "-n auto" in command
    assert "tests/" in command
    assert "--cov=hooks" in command
    assert "-m unit" not in command
