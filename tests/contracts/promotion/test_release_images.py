import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_ci_hosted_lanes import _resolve

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github/workflows"
IMAGES = {"swarm-image": "swarm-image.yml", "worker-image": "swarm-node-image.yml"}
DEV_PUSH = {"event_name": "push", "ref": "refs/heads/dev", "run_id": 1000}


def _load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _step(job, name):
    [step] = [step for step in job["steps"] if step.get("name") == name]
    return step


def test_the_promote_job_exposes_the_release_version_and_the_promoted_main_commit():
    workflow = _load("promote.yml")
    assert workflow["jobs"]["promote"]["outputs"] == {
        "version": "${{ steps.version.outputs.next }}",
        "main": "${{ steps.promote.outputs.main }}",
    }


@pytest.mark.parametrize("job,callee", IMAGES.items())
def test_a_promotion_publishes_each_image_from_the_promoted_main_commit(job, callee):
    image = _load("promote.yml")["jobs"][job]
    assert image["needs"] == "promote"
    assert image["if"] == "needs.promote.outputs.version != ''"
    assert image["uses"] == f"./.github/workflows/{callee}"
    assert image["with"] == {
        "version": "${{ needs.promote.outputs.version }}",
        "ref": "${{ needs.promote.outputs.main }}",
    }
    assert image["permissions"] == {"contents": "read", "packages": "write"}


def test_the_tests_promote_job_grants_the_nested_image_jobs_their_registry_write():
    promote = _load("test.yml")["jobs"]["promote"]
    assert promote["permissions"] == {"contents": "read", "packages": "write"}


@pytest.mark.parametrize("callee", IMAGES.values())
def test_each_image_workflow_takes_the_release_version_and_ref_when_called(callee):
    inputs = _load(callee)[True]["workflow_call"]["inputs"]
    assert inputs == {
        "version": {"type": "string", "required": False, "default": ""},
        "ref": {"type": "string", "required": False, "default": ""},
    }


@pytest.mark.parametrize("callee", IMAGES.values())
def test_a_release_build_never_shares_the_dev_push_concurrency_group(callee):
    block = _load(callee)["concurrency"]
    dev = _resolve(block, **DEV_PUSH)
    release = _resolve(block, inputs={"version": "2.9.1", "ref": "abc"}, **DEV_PUSH)
    assert release["group"] != dev["group"]
    assert release["group"].endswith("release")
    assert not release["cancel-in-progress"]


def test_the_swarm_release_checks_out_main_and_tags_the_version_and_release(tmp_path):
    job = _load("swarm-image.yml")["jobs"]["publish"]
    checkout = job["steps"][0]
    assert checkout["with"]["ref"] == "${{ inputs.ref || github.sha }}"
    tags = job["steps"][1]
    assert tags["env"]["VERSION"] == "${{ inputs.version }}"

    def run(version):
        output = tmp_path / f"output-{version or 'dev'}"
        env = {
            **os.environ,
            **tags["env"],
            "VERSION": version,
            "GITHUB_SHA": "123456789abcdef",
            "GITHUB_OUTPUT": str(output),
        }
        subprocess.run(["bash", "-e", "-c", tags["run"]], env=env, check=True)
        return output.read_text().splitlines()

    assert run("2.9.1") == [
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


def test_the_worker_release_builds_main_and_moves_the_version_and_release_tags():
    job = _load("swarm-node-image.yml")["jobs"]["image"]
    assert job["env"]["REVISION"] == "${{ inputs.ref || github.sha }}"
    assert job["steps"][0]["with"]["ref"] == "${{ inputs.ref || github.sha }}"
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    assert "$GITHUB_SHA" not in runs
    assert 'publish.sh "$CANDIDATE" "$REPOSITORY" "$REVISION" "$out"' in runs
    release = _step(job, "Move the version and release tags to the published digest")
    assert release["if"] == "inputs.version != ''"
    assert release["env"] == {"VERSION": "${{ inputs.version }}"}
    assert release["run"].splitlines() == [
        'digest="$(jq -r .digest "$RUNNER_TEMP/worker-image-attestation/candidate/promoted.json")"',
        'docker buildx imagetools create --tag "$REPOSITORY:$VERSION" --tag "$REPOSITORY:release" "$REPOSITORY@$digest"',
    ]
    dev = _step(job, "Move the floating dev tag to the published digest")
    assert dev["if"].endswith("&& inputs.version == ''")
    names = [step.get("name") for step in job["steps"]]
    assert names.index("Publish the qualified candidate") < names.index(release["name"])
