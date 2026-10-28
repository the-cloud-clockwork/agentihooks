import json
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


def test_the_nats_client_is_an_optional_extra_outside_a_plain_install():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]
    extras = project["optional-dependencies"]
    assert extras["nats"] == ["nats-py>=2.9"]
    assert "nats-py>=2.9" in extras["all"]
    assert not [requirement for requirement in project["dependencies"] if requirement.startswith("nats")]


def test_the_swarm_image_lock_pins_the_nats_extra_with_hashes():
    root = Path(__file__).parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    wanted = {requirement.split(">=")[0].lower() for requirement in project["optional-dependencies"]["nats"]}
    lock = (root / "docker/swarm/requirements.lock").read_text().split("\n")
    pins = {line.split("==")[0].lower(): line for line in lock if "==" in line and not line.startswith(" ")}
    assert wanted == {"nats-py"}
    assert wanted <= pins.keys()
    hashes = lock[lock.index(pins["nats-py"]) + 1 :][:2]
    assert all(line.startswith("    --hash=sha256:") for line in hashes)


def test_the_swarm_image_installs_the_herdr_client_the_worker_lock_pins():
    root = Path(__file__).parents[1]
    dockerfile = (root / "Dockerfile").read_text()
    ignored = (root / ".dockerignore").read_text().splitlines()
    assert "!docker/swarm-node/versions.lock" in ignored
    assert "!docker/swarm-node/requirements.lock" in ignored
    assert "COPY docker/swarm-node/versions.lock docker/swarm-node/requirements.lock /opt/swarm-node/" in dockerfile
    assert 'worker_image.py install --tool herdr --architecture "$TARGETARCH"' in dockerfile
    assert "COPY --link=true --from=herdr /usr/local/bin/herdr /usr/local/bin/herdr" in dockerfile
    assert "herdrdev/herdr" not in dockerfile
    assert "sha256:" not in dockerfile.split("AS herdr")[1]
    bases = [line.split()[1] for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert len(bases) >= 3
    assert set(bases) == {"mirror.gcr.io/library/python:3.12-slim-bookworm"}
    assert "worker_image.py shell-packages --package openssh-client)" in dockerfile
    assert '["debian_snapshot"]' in dockerfile
    keyed = json.loads((root / ".github/input-keys.json").read_text())["helm-kind"]["paths"]
    assert "docker/swarm-node" in keyed
