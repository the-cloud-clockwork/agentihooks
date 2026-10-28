import re
import shlex
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
LOCK = ".github/test-requirements.lock"


def _unlocked_installs(root):
    failures = []
    for path in sorted((root / ".github").rglob("*")):
        if path.suffix in {".yml", ".yaml"}:
            document = yaml.safe_load(path.read_text()) or {}
            jobs = document.get("jobs", {})
            steps = [step for job in jobs.values() for step in job.get("steps", [])]
            steps += document.get("runs", {}).get("steps", [])
            scripts = [step.get("run", "") for step in steps]
        elif path.suffix == ".sh":
            scripts = [path.read_text()]
        else:
            continue
        for script in scripts:
            script = script.replace("\\\n", " ")
            for line in script.splitlines():
                if not re.search(r"\bpip\s+install\b", line):
                    continue
                if not re.search(r"pytest|mutmut|coverage|\[[^]]*\bdev\b", line):
                    continue
                tokens = shlex.split(line)
                constrained = any(
                    token in {"-c", "--constraint"}
                    and index + 1 < len(tokens)
                    and tokens[index + 1].endswith(LOCK)
                    or token.startswith("--constraint=")
                    and token.endswith(LOCK)
                    for index, token in enumerate(tokens)
                )
                if not constrained:
                    failures.append(f"{path.relative_to(root)}: unlocked test tool install: {line.strip()}")
    return failures


def test_ci_test_tool_installs_use_the_repository_lock():
    assert not (failures := _unlocked_installs(ROOT)), "\n".join(failures)


@pytest.mark.parametrize("command", ["uv pip install --system -e '.[dev,all]'", "python -m pip install pytest mutmut"])
def test_dependency_inventory_rejects_a_planted_unlocked_install(tmp_path, command):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "plant.yml").write_text(yaml.safe_dump({"jobs": {"plant": {"steps": [{"run": command}]}}}))
    assert len(_unlocked_installs(tmp_path)) == 1
    (workflows / "plant.yml").write_text(
        yaml.safe_dump({"jobs": {"plant": {"steps": [{"run": f"{command} --constraint {LOCK}"}]}}})
    )
    assert _unlocked_installs(tmp_path) == []


def test_repository_lock_pins_pytest_plugins_and_mutmut():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    tools = {
        Requirement(value).name
        for value in project["project"]["optional-dependencies"]["dev"]
        if Requirement(value).name.startswith("pytest") or Requirement(value).name == "mutmut"
    } | {"coverage"}
    requirements = [
        Requirement(line.strip())
        for line in (ROOT / LOCK).read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for tool in tools:
        matches = [requirement for requirement in requirements if requirement.name == tool]
        assert matches, f"{tool} is missing from the repository lock"
        assert all(len(requirement.specifier) == 1 for requirement in matches)
        assert all(next(iter(requirement.specifier)).operator == "==" for requirement in matches)
