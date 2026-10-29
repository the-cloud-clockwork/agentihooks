import re
import tomllib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[1]
_LOCK = ".github/test-requirements.lock"


def _unlocked_installs(workflow):
    unlocked = []
    for name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            for line in step.get("run", "").splitlines():
                if not re.search(r"\bpip install\b", line):
                    continue
                if not re.search(r"\[[^\]]*\bdev\b[^\]]*\]|\b(?:pytest[\w-]*|mutmut)\b", line):
                    continue
                directory = step.get(
                    "working-directory", job.get("defaults", {}).get("run", {}).get("working-directory", ".")
                )
                nested = "head/" if "./head[" in line else ""
                expected = f"--constraint {nested}{_LOCK}"
                if expected not in line:
                    unlocked.append(f"{name}:{step.get('name', directory)}: {line.strip()}")
    return unlocked


def test_every_ci_test_tool_install_uses_the_repository_lock():
    unlocked = []
    for path in sorted((_ROOT / ".github/workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        unlocked.extend(f"{path.name}:{item}" for item in _unlocked_installs(workflow))
    assert unlocked == []


@pytest.mark.parametrize(
    "install",
    [
        "uv pip install -e '.[dev,all]'",
        'uv pip install "pytest>=8"',
        "python -m pip install mutmut==3.6.0",
        "uv pip install pytest-cov",
    ],
)
def test_an_unlocked_test_install_is_rejected(install):
    workflow = {"jobs": {"plant": {"steps": [{"name": "Planted unlocked install", "run": install}]}}}
    assert len(_unlocked_installs(workflow)) == 1
    workflow["jobs"]["plant"]["steps"][0]["run"] = install.replace("pip install", f"pip install --constraint {_LOCK}")
    assert _unlocked_installs(workflow) == []


def test_offline_and_online_installs_both_need_the_lock():
    workflow = {
        "jobs": {
            "plant": {
                "steps": [
                    {
                        "run": f"uv pip install --constraint {_LOCK} --offline -e '.[dev,all]'\nuv pip install -e '.[dev,all]'"
                    }
                ]
            }
        }
    }
    assert len(_unlocked_installs(workflow)) == 1


def test_the_committed_lock_pins_every_declared_test_tool():
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    declared = project["project"]["optional-dependencies"]["dev"]
    tools = {re.split(r"[<>=!\[]", spec)[0].lower() for spec in declared}
    pins = set()
    for line in (_ROOT / _LOCK).read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        assert re.match(r"^[a-zA-Z0-9_.-]+==[^\s;]+(?:\s*;.*)?$", line), line
        pins.add(line.split("==")[0].lower())
    assert tools <= pins


def test_test_environment_cache_keys_include_the_lock():
    for path in sorted((_ROOT / ".github/workflows").glob("*.yml")):
        for name, job in yaml.safe_load(path.read_text())["jobs"].items():
            for step in job.get("steps", []):
                key = str(step.get("with", {}).get("key", ""))
                if key.startswith("venv-"):
                    assert _LOCK in key, f"{path.name}:{name}"
