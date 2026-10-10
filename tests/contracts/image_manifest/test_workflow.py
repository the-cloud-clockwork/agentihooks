import json
import os
import re
import subprocess
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
    gate = (
        "(github.event_name == 'push' || inputs.publish)"
        " && (github.ref == 'refs/heads/dev' || startsWith(github.ref, 'refs/heads/diffcheck/'))"
    )

    assert named(workflow, "Log in to the registry")["if"] == gate
    assert named(workflow, "Publish the qualified candidate")["if"] == gate


def test_the_attestation_is_uploaded_even_after_a_failure(workflow):
    upload = named(workflow, "Upload the attestation")

    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "${{ runner.temp }}/worker-image-attestation"


def test_checkout_keeps_no_credential_in_the_repository(workflow):
    [checkout] = [step for step in steps(workflow) if step.get("uses", "").startswith("actions/checkout@")]

    assert checkout["with"] == {"persist-credentials": False}


COMMIT = "c" * 40
TESTED = "sha256:" + "1" * 64
OLD = "sha256:" + "2" * 64
NEW = "sha256:" + "3" * 64
STUB = """#!/usr/bin/env bash
echo "$*" >> "$DOCKER_LOG"
case "$*" in
"buildx imagetools inspect --format {{json .Manifest}} "*)
    if [[ "$EXISTING" == yes ]]; then echo "{\\"digest\\": \\"$OLD\\"}"; exit 0; fi
    echo "ERROR: $EXISTING" >&2
    exit 1;;
"buildx imagetools inspect --raw "*) echo "{\\"config\\": {\\"digest\\": \\"$CONFIG\\"}}";;
"image inspect "*) echo "$REPO@$NEW";;
esac
"""


def publish(tmp_path, existing, config=TESTED):
    bin_dir, output = tmp_path / "bin", tmp_path / "out"
    bin_dir.mkdir()
    output.mkdir()
    (bin_dir / "docker").write_text(STUB)
    (bin_dir / "docker").chmod(0o755)
    made = {"image_id": TESTED, "promotable": True, "refusals": [], "digest": None, "tags": []}
    (output / "attestation.json").write_text(json.dumps(made))
    environ = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "DOCKER_LOG": str(tmp_path / "docker.log"),
        "EXISTING": existing,
        "CONFIG": config,
        "OLD": OLD,
        "NEW": NEW,
        "REPO": "ghcr.io/o/worker",
    }
    script = ROOT / "docker/swarm-node/publish.sh"
    done = subprocess.run(
        ["bash", str(script), "candidate:x", "ghcr.io/o/worker", COMMIT, str(output)],
        env=environ,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    log = (tmp_path / "docker.log").read_text().splitlines()
    promoted = output / "promoted.json"
    return done, log, json.loads(promoted.read_text()) if promoted.exists() else None


def test_publish_pushes_an_absent_commit_tag_and_records_its_digest(tmp_path):
    done, log, promoted = publish(tmp_path, "manifest unknown: not found")

    tag = f"ghcr.io/o/worker:sha-{COMMIT}"
    assert done.returncode == 0, done.stderr
    assert f"tag candidate:x {tag}" in log and f"push {tag}" in log
    assert (promoted["digest"], promoted["promoted"], promoted["tags"]) == (NEW, True, [tag])


def test_publish_never_retags_an_existing_commit_tag(tmp_path):
    done, log, promoted = publish(tmp_path, "yes")

    assert done.returncode == 0, done.stderr
    assert not [line for line in log if line.startswith(("tag ", "push "))]
    assert (promoted["digest"], promoted["promoted"], promoted["replayed"]) == (OLD, False, True)


def test_publish_refuses_an_existing_tag_holding_another_image(tmp_path):
    done, log, promoted = publish(tmp_path, "yes", config="sha256:" + "4" * 64)

    assert done.returncode == 1
    assert "registry image is not the tested image" in done.stderr
    assert promoted is None and not [line for line in log if line.startswith("push ")]


def test_publish_stops_when_the_registry_cannot_answer(tmp_path):
    done, log, promoted = publish(tmp_path, "unauthorized")

    assert done.returncode == 1
    assert "nothing pushed" in done.stderr
    assert promoted is None and not [line for line in log if line.startswith(("tag ", "push "))]
