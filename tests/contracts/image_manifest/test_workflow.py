import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/swarm-node-image.yml"
TOKEN = re.compile(r"secrets\.|github\.token", re.IGNORECASE)


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def steps(workflow):
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


def named(workflow, name):
    [step] = [step for step in steps(workflow) if step.get("name") == name]
    return step


def test_dispatch_and_dev_pushes_trigger_it(workflow):
    triggers = workflow[True]

    assert triggers["push"]["branches"] == ["dev", "diffcheck/**"]
    assert triggers["workflow_dispatch"]["inputs"]["publish"]["type"] == "boolean"
    assert "pull_request" not in triggers


def test_proof_branches_publish_only_to_the_proof_repository(workflow):
    repository = workflow["jobs"]["image"]["env"]["REPOSITORY"]

    assert repository == (
        "${{ startsWith(github.ref, 'refs/heads/diffcheck/')"
        " && 'ghcr.io/the-cloud-clockwork/agentihooks-worker-proof'"
        " || 'ghcr.io/the-cloud-clockwork/agentihooks-worker' }}"
    )


def test_every_job_runs_on_a_github_hosted_runner_without_swarm_services(workflow):
    for job in workflow["jobs"].values():
        assert job["runs-on"] == "ubuntu-latest"
        assert "uses" not in job and "services" not in job and "container" not in job
    text = WORKFLOW.read_text()
    assert not re.search(r"anton|self-hosted|LEDGER|REDIS|AGENTIHOOKS_SWARM", text)


def test_only_the_registry_login_holds_a_credential(workflow):
    holders = [step["name"] for step in steps(workflow) if TOKEN.search(str(step))]

    assert holders == ["Log in to the registry"]
    assert named(workflow, "Log in to the registry")["with"]["password"] == "${{ secrets.GITHUB_TOKEN }}"


def test_builds_pass_only_the_source_revision(workflow):
    builds = [step["run"] for step in steps(workflow) if "docker build" in step.get("run", "")]

    assert builds
    for run in builds:
        assert re.findall(r"--build-arg (\w+)", run) in (["SOURCE_REVISION"], ["WORKER_IMAGE"])


def test_qualification_precedes_login_and_publication(workflow):
    names = [step.get("name") for step in steps(workflow)]

    order = [
        "Build the candidate",
        "Build the incompatible herdr fixture",
        "Refuse the incompatible herdr fixture",
        "Qualify the candidate",
        "Requalify the candidate independently",
        "Log in to the registry",
        "Publish the qualified candidate",
    ]
    assert [name for name in names if name in order] == order


def test_publication_runs_only_on_dev_pushes_or_an_explicit_dispatch(workflow):
    gate = "github.event_name == 'push' || inputs.publish"

    assert named(workflow, "Log in to the registry")["if"] == gate
    assert named(workflow, "Publish the qualified candidate")["if"] == gate


def test_the_attestation_is_uploaded_even_after_a_failure(workflow):
    upload = named(workflow, "Upload the attestation")

    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "${{ runner.temp }}/worker-image-attestation"


def test_the_publish_script_never_retags_an_existing_commit_tag():
    script = (ROOT / "docker/swarm-node/publish.sh").read_text()

    assert "--existing" in script
    assert script.index("imagetools inspect") < script.index("docker push")
