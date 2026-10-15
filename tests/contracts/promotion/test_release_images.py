import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_ci_hosted_lanes import _resolve

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github/workflows"
IMAGES = ("swarm-image.yml", "swarm-node-image.yml")
TAG = {"event_name": "release", "ref": "refs/tags/v2.9.1", "run_id": 1000}


def _load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _step(job, name):
    [step] = [step for step in job["steps"] if step.get("name") == name]
    return step


def _job(name):
    [job] = _load(name)["jobs"].values()
    return job


def test_promotion_creates_the_release_with_the_app_token_so_its_event_starts_workflows():
    job = _load("promote.yml")["jobs"]["promote"]
    release = _step(job, "Create the release with generated notes")
    assert release["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert "gh release create" in release["run"]


@pytest.mark.parametrize("name", IMAGES)
def test_each_image_workflow_publishes_on_a_published_release_in_its_own_run(name):
    assert _load(name)[True]["release"] == {"types": ["published"]}
    assert not _resolve(_load(name)["concurrency"], **TAG)["cancel-in-progress"]


@pytest.mark.parametrize("name", IMAGES)
def test_a_release_build_refuses_a_tag_that_main_does_not_hold(name):
    job = _job(name)
    checkout = job["steps"][0]
    assert checkout["with"]["fetch-depth"] == "${{ github.event_name == 'release' && '0' || '1' }}"
    guard = _step(job, "Require a release tag on main")
    assert guard["if"] == "github.event_name == 'release'"
    assert guard["run"] == 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main'
    names = [step.get("name") for step in job["steps"]]
    assert names.index(guard["name"]) == 1


def test_the_swarm_release_tags_the_version_and_release(tmp_path):
    job = _job("swarm-image.yml")
    assert "github.event_name == 'release'" in job["if"]
    tags = next(step for step in job["steps"] if step.get("id") == "tags")
    assert tags["env"]["RELEASE"] == "${{ github.event.release.tag_name }}"

    def run(release):
        output = tmp_path / f"output-{release or 'dev'}"
        env = {
            **os.environ,
            **tags["env"],
            "RELEASE": release,
            "GITHUB_SHA": "123456789abcdef",
            "GITHUB_OUTPUT": str(output),
        }
        subprocess.run(["bash", "-e", "-c", tags["run"]], env=env, check=True)
        return output.read_text().splitlines()

    assert run("v2.9.1") == [
        "tags<<EOF",
        "ghcr.io/the-cloud-clockwork/agentihooks-swarm:2.9.1",
        "ghcr.io/the-cloud-clockwork/agentihooks-swarm:release",
        "EOF",
    ]
    assert run("") == [
        "tags<<EOF",
        "ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev",
        "ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev-1234567",
        "EOF",
    ]


def test_the_worker_release_publishes_and_moves_the_version_and_release_tags_but_never_dev():
    job = _job("swarm-node-image.yml")
    for name in ("Log in to the registry", "Publish the qualified candidate"):
        assert _step(job, name)["if"].startswith("github.event_name == 'release' || ")
    release = _step(job, "Move the version and release tags to the published digest")
    assert release["if"] == "github.event_name == 'release'"
    assert release["env"] == {"RELEASE": "${{ github.event.release.tag_name }}"}
    assert release["run"].splitlines() == [
        'digest="$(jq -r .digest "$RUNNER_TEMP/worker-image-attestation/candidate/promoted.json")"',
        'docker buildx imagetools create --tag "$REPOSITORY:${RELEASE#v}" --tag "$REPOSITORY:release" "$REPOSITORY@$digest"',
    ]
    dev = _step(job, "Move the floating dev tag to the published digest")
    assert "github.ref == 'refs/heads/dev'" in dev["if"]
    names = [step.get("name") for step in job["steps"]]
    assert names.index("Publish the qualified candidate") < names.index(release["name"])
