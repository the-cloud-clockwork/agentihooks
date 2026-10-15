import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_ci_anton_runners import resolve

pytestmark = pytest.mark.unit


def _publish():
    path = Path(__file__).resolve().parents[1] / ".github/workflows/swarm-image.yml"
    return yaml.safe_load(path.read_text())["jobs"]["publish"]


@pytest.mark.parametrize(
    "actor,triggering_actor,runner",
    [
        ("nestorcolt", "nestorcolt", "anton-oss"),
        ("agenticore-app[bot]", "agenticore-app[bot]", "anton-oss"),
        ("tcc-main-ci[bot]", "tcc-main-ci[bot]", "anton-oss"),
        ("github-merge-queue[bot]", "github-merge-queue[bot]", "anton-oss"),
        ("outsider", "nestorcolt", "ubuntu-latest"),
        ("nestorcolt", "outsider", "ubuntu-latest"),
        ("github-merge-queue[bot]", "nestorcolt", "anton-oss"),
        ("github-merge-queue[bot]", "outsider", "ubuntu-latest"),
    ],
)
def test_swarm_publish_uses_the_public_docker_runner_only_for_trusted_actors(actor, triggering_actor, runner):
    github = {"event_name": "push", "ref": "refs/heads/dev", "actor": actor, "triggering_actor": triggering_actor}
    assert resolve(_publish()["runs-on"], **github) == runner


def test_swarm_publish_builds_locally_and_pushes_the_existing_tags_and_cache(tmp_path):
    job = _publish()
    assert "uses" not in job
    assert (
        job["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/dev' || github.event_name == 'release'"
    )
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    steps = job["steps"]
    checkout, on_main, tags, login, builder, build = steps
    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["with"]["persist-credentials"] is False
    assert login["uses"] == "docker/login-action@v3"
    assert login["with"] == {
        "registry": "ghcr.io",
        "username": "${{ github.actor }}",
        "password": "${{ secrets.GITHUB_TOKEN }}",
    }
    assert builder["uses"] == "docker/setup-buildx-action@v3"
    assert build["uses"] == "docker/build-push-action@v6"
    assert build["with"] == {
        "context": ".",
        "file": "Dockerfile",
        "push": True,
        "tags": "${{ steps.tags.outputs.tags }}",
        "cache-from": "type=gha,scope=swarm",
        "cache-to": "type=gha,mode=max,scope=swarm",
    }
    output = tmp_path / "output"
    subprocess.run(
        ["bash", "-e", "-c", tags["run"]],
        env={**os.environ, **tags["env"], "RELEASE": "", "GITHUB_SHA": "123456789abcdef", "GITHUB_OUTPUT": str(output)},
        check=True,
    )
    assert output.read_text().splitlines() == [
        "tags<<EOF",
        "ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev",
        "ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev-1234567",
        "EOF",
    ]
