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
            scripts = [
                (step.get("run", ""), document.get("env", {}) | job.get("env", {}) | step.get("env", {}))
                for job in document.get("jobs", {}).values()
                for step in job.get("steps", [])
            ]
            scripts += [
                (step.get("run", ""), step.get("env", {})) for step in document.get("runs", {}).get("steps", [])
            ]
        elif path.suffix == ".sh":
            scripts = [(path.read_text(), {})]
        else:
            continue
        for script, environment in scripts:
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
                constraint = environment.get("UV_CONSTRAINT", "")
                if re.search(r"\buv\s+pip\s+install\b", line) and constraint.endswith(".github/test-constraints.txt"):
                    constrained |= (
                        "--constraint test-requirements.lock"
                        in (root / ".github/test-constraints.txt").read_text().splitlines()
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


@pytest.mark.parametrize(
    "workflow,job,checkout",
    [("mutation-line-selection-proof.yml", "compare", "head"), ("ledger-row-memory.yml", "replay", "proof")],
)
def test_historical_replays_use_the_current_checkouts_lock(workflow, job, checkout):
    document = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    steps = document["jobs"][job]["steps"]
    current = next(step for step in steps if step.get("with", {}).get("path") == checkout)
    assert "ref" not in current["with"]
    install = next(step for step in steps if "pip install" in step.get("run", ""))
    assert install["working-directory"] != checkout
    tokens = shlex.split(install["run"])
    assert tokens[tokens.index("--constraint") + 1] == f"../{checkout}/{LOCK}"


@pytest.mark.parametrize("scope", ["workflow", "job", "step"])
@pytest.mark.parametrize("installer", ["uv pip", "python -m pip"])
def test_inherited_constraints_must_include_the_generated_lock(tmp_path, scope, installer):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    constraints = workflows.parent / "test-constraints.txt"
    constraints.write_text("pytest<9.2\n")
    step = {"run": f"{installer} install pytest mutmut"}
    job = {"steps": [step]}
    workflow = {"jobs": {"plant": job}}
    {"workflow": workflow, "job": job, "step": step}[scope]["env"] = {
        "UV_CONSTRAINT": "head/.github/test-constraints.txt"
    }
    (workflows / "plant.yml").write_text(yaml.safe_dump(workflow))
    assert len(_unlocked_installs(tmp_path)) == 1
    constraints.write_text("pytest<9.2\n--constraint test-requirements.lock\n")
    assert len(_unlocked_installs(tmp_path)) == (0 if installer == "uv pip" else 1)
