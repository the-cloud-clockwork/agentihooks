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
    jobs = _load(name)["jobs"]
    return jobs.get("image") or jobs["publish"]


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
    position = job["steps"].index(guard)
    publishing = [i for i, step in enumerate(job["steps"]) if "login-action" in step.get("uses", "")]
    assert publishing
    assert position < publishing[0]


def _guard(tmp_path, ref_name, on_main=True):
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for tag in ("v2.9.0", "v2.10.0", "v2.9.1"):
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", tag], check=True)
        subprocess.run([*git, "tag", tag], check=True)
    subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
    if not on_main:
        subprocess.run([*git, "checkout", "-q", "--orphan", "side"], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "side"], check=True)
        subprocess.run([*git, "tag", "v9.0.0"], check=True)
    sha = subprocess.run([*git, "rev-parse", f"{ref_name}^{{commit}}"], capture_output=True, text=True, check=True)
    output = tmp_path / "output"
    run = _step(_job("swarm-image.yml"), "Require a release tag on main")["run"]
    env = {**os.environ, "GITHUB_SHA": sha.stdout.strip(), "GITHUB_REF_NAME": ref_name, "GITHUB_OUTPUT": str(output)}
    done = subprocess.run(["bash", "-e", "-c", run], cwd=tmp_path, env=env)
    return done.returncode, output.read_text() if output.exists() else ""


def test_both_image_workflows_share_the_release_guard():
    runs = {name: _step(_job(name), "Require a release tag on main")["run"] for name in IMAGES}
    assert len(set(runs.values())) == 1


@pytest.mark.parametrize(("ref_name", "latest"), [("v2.10.0", "true"), ("v2.9.1", "false"), ("v2.9.0", "false")])
def test_only_the_newest_stable_tag_on_main_moves_the_release_tag(tmp_path, ref_name, latest):
    assert _guard(tmp_path, ref_name) == (0, f"latest={latest}\n")


def test_a_release_tag_off_main_publishes_nothing(tmp_path):
    code, output = _guard(tmp_path, "v9.0.0", on_main=False)
    assert code != 0
    assert output == ""


def test_the_swarm_release_tags_the_version_and_release(tmp_path):
    job = _job("swarm-image.yml")
    assert "github.event_name == 'release'" in job["if"]
    tags = next(step for step in job["steps"] if step.get("id") == "tags")
    assert tags["env"]["RELEASE"] == "${{ github.event.release.tag_name }}"

    assert tags["env"]["LATEST"] == "${{ steps.main.outputs.latest }}"

    def run(release, latest=""):
        output = tmp_path / f"output-{release or 'dev'}-{latest}"
        env = {
            **os.environ,
            **tags["env"],
            "RELEASE": release,
            "LATEST": latest,
            "GITHUB_SHA": "123456789abcdef",
            "GITHUB_OUTPUT": str(output),
        }
        subprocess.run(["bash", "-e", "-c", tags["run"]], env=env, check=True)
        return output.read_text().splitlines()

    assert run("v2.9.1", "true") == [
        "tags<<EOF",
        "ghcr.io/the-cloud-clockwork/agentihooks-swarm:2.9.1",
        "ghcr.io/the-cloud-clockwork/agentihooks-swarm:release",
        "EOF",
    ]
    assert run("v2.9.0", "false") == [
        "tags<<EOF",
        "ghcr.io/the-cloud-clockwork/agentihooks-swarm:2.9.0",
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
    release = _step(job, "Tag the release version and move the release tag when it is the newest")
    assert release["if"] == "github.event_name == 'release'"
    assert release["env"] == {
        "RELEASE": "${{ github.event.release.tag_name }}",
        "LATEST": "${{ steps.main.outputs.latest }}",
    }
    assert release["run"].splitlines() == [
        'digest="$(jq -r .digest "$RUNNER_TEMP/worker-image-attestation/candidate/promoted.json")"',
        'tags=(--tag "$REPOSITORY:${RELEASE#v}")',
        'if [[ "$LATEST" == true ]]; then tags+=(--tag "$REPOSITORY:release"); fi',
        'docker buildx imagetools create "${tags[@]}" "$REPOSITORY@$digest"',
    ]
    dev = _step(job, "Move the floating dev tag to the published digest")
    assert "github.ref == 'refs/heads/dev'" in dev["if"]
    names = [step.get("name") for step in job["steps"]]
    assert names.index("Publish the qualified candidate") < names.index(release["name"])
