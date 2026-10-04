import re
import tomllib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent


def _pytest_command() -> str:
    for line in (_ROOT / ".github/workflows/test.yml").read_text().splitlines():
        if "pytest" in line and line.strip().startswith("run:"):
            return line
    raise AssertionError("no pytest step in test.yml")


@pytest.mark.parametrize("plugin", ["pytest-xdist", "pytest-split"])
def test_dev_extra_declares_test_plugin(plugin):
    dev = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]["dev"]
    assert any(dep.startswith(plugin) for dep in dev)


def test_workflow_runs_whole_suite_in_parallel_with_coverage():
    command = _pytest_command()
    assert "-n auto" in command
    assert "tests/" in command
    assert "--cov=hooks" in command
    assert "-m unit" not in command


def _setup_python_steps(job: str) -> list[dict]:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    return [s for s in workflow["jobs"][job]["steps"] if s.get("uses", "").startswith("actions/setup-python")]


@pytest.mark.parametrize("job", ["unit", "lint"])
def test_setup_python_caches_pip_keyed_on_pyproject(job):
    (step,) = _setup_python_steps(job)
    assert step["with"]["cache"] == "pip"
    assert step["with"]["cache-dependency-path"] == "pyproject.toml"


def test_unit_matrix_runs_one_shard_per_split():
    command = _pytest_command()
    splits = int(re.search(r"--splits (\d+)", command).group(1))
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    assert splits > 1
    assert workflow["jobs"]["unit"]["strategy"]["matrix"]["shard"] == list(range(1, splits + 1))
    assert "--group ${{ matrix.shard }}" in command


def test_tests_run_on_pull_requests_into_dev_and_main():
    triggers = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())[True]
    assert set(triggers["pull_request"]["branches"]) == {"dev", "main"}
    assert triggers["push"]["branches"] == ["dev"]
