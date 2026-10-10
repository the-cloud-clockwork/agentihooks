import tomllib
from pathlib import Path


def test_ledger_dependencies_exclude_workstation_tools():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]
    requirements = project["optional-dependencies"]["ledger"]
    names = {requirement.split(">=")[0] for requirement in requirements}
    assert {"jsonschema", "redis", "requests"} <= names
    assert not names & {"mcp", "playwright", "tree-sitter", "opentelemetry-sdk"}
    assert len(requirements) < len(project["dependencies"])


def test_swarm_image_installs_every_ledger_dependency_from_its_hashed_lock():
    root = Path(__file__).parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    wanted = {requirement.split(">=")[0].lower() for requirement in project["optional-dependencies"]["ledger"]}
    lock = (root / "docker/swarm/requirements.lock").read_text()
    pinned = {line.split("==")[0].lower() for line in lock.splitlines() if "==" in line and not line.startswith(" ")}
    assert wanted <= pinned
    assert "requests" in pinned
    assert all("--hash=sha256:" in block for block in lock.split("==")[1:])
    dockerfile = (root / "Dockerfile").read_text()
    assert "--require-hashes -r requirements.lock" in dockerfile
    assert "!docker/swarm/requirements.lock" in (root / ".dockerignore").read_text().splitlines()
