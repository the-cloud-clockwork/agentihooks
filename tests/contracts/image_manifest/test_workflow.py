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
        "Build the planted credential fixture",
        "Refuse the planted credential fixture",
        "Scan the candidate for credentials",
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


@pytest.mark.parametrize("reply", ["ghcr.io/o/worker:sha-x: not found", "MANIFEST_UNKNOWN: manifest unknown"])
def test_publish_pushes_an_absent_commit_tag_and_records_its_digest(tmp_path, reply):
    done, log, promoted = publish(tmp_path, reply)

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


SCANNER = """#!/usr/bin/env bash
echo "$*" >> "$DOCKER_LOG"
for arg in "$@"; do
    case "$arg" in *:/out) out="${arg%:/out}";; esac
done
if [[ -n "$REPORT" ]]; then printf '%s' "$REPORT" > "$out/secrets.json"; fi
exit "$STATUS"
"""
CLEAN = json.dumps({"Results": [{"Target": "opt/app/a.py", "Class": "secret"}]})
LEAK = json.dumps(
    {"Results": [{"Target": "opt/agentihooks/.build-token", "Secrets": [{"RuleID": "github-app-token"}]}]}
)


def scan(tmp_path, status, report):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(SCANNER)
    (bin_dir / "docker").chmod(0o755)
    environ = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "DOCKER_LOG": str(tmp_path / "docker.log"),
        "STATUS": str(status),
        "REPORT": report,
    }
    done = subprocess.run(
        ["bash", str(ROOT / "docker/swarm-node/scan.sh"), "candidate:x", str(tmp_path / "out")],
        env=environ,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return done, (tmp_path / "docker.log").read_text()


def test_scan_reads_every_layer_and_the_image_config_for_secrets(tmp_path):
    done, log = scan(tmp_path, 0, CLEAN)

    assert done.returncode == 0, done.stderr
    assert "--scanners secret --image-config-scanners secret --exit-code 1" in log
    assert "/trivy-secret.yaml:/etc/trivy-secret.yaml:ro" in log and "--secret-config /etc/trivy-secret.yaml" in log
    assert log.rstrip().endswith("candidate:x")
    assert "no credentials found in candidate:x" in done.stdout


def test_the_scan_allows_only_published_python_package_descriptions():
    config = yaml.safe_load((ROOT / "docker/swarm-node/trivy-secret.yaml").read_text())
    [rule] = config.pop("allow-rules")
    path = re.compile(rule["path"])

    assert config == {} and set(rule) == {"id", "description", "path"}
    assert path.search("/opt/venv/lib/python3.12/site-packages/pyjwt-2.15.1.dist-info/METADATA")
    for other in (
        "/opt/agentihooks/.build-token",
        "/root/.git-credentials",
        "/opt/venv/lib/python3.12/site-packages/pyjwt-2.15.1.dist-info/METADATA.bak",
        "/opt/venv/lib/python3.12/site-packages/jwt/api_jwt.py",
    ):
        assert not path.search(other)


def test_scan_refuses_an_image_holding_a_credential(tmp_path):
    done, _ = scan(tmp_path, 1, LEAK)

    assert done.returncode == 1
    assert "github-app-token opt/agentihooks/.build-token" in done.stderr
    assert (tmp_path / "out/findings.txt").read_text() == "github-app-token opt/agentihooks/.build-token\n"


@pytest.mark.parametrize(
    ("status", "report"),
    [(1, ""), (2, CLEAN), (1, CLEAN)],
    ids=["no-report", "scanner-error", "exit-without-findings"],
)
def test_scan_is_red_when_the_scanner_cannot_finish(tmp_path, status, report):
    done, _ = scan(tmp_path, status, report)

    assert done.returncode == 2
    assert "credential scan" in done.stderr


def test_the_planted_credential_is_generated_at_build_time_and_refused_before_publication(workflow):
    fixture = (ROOT / "tests/contracts/image_manifest/planted-credential/Dockerfile").read_text()
    refusal = named(workflow, "Refuse the planted credential fixture")["run"]

    assert not re.search(r"gh[pousr]_[0-9A-Za-z]{36}", fixture)
    assert "'ghs_' +" in fixture and "> /opt/agentihooks/.build-token" in fixture
    assert 'bash docker/swarm-node/scan.sh "$PLANTED"' in refusal and '"$status" != 1' in refusal
    assert "grep -qE '^github-app-token (.*/)?opt/agentihooks/\\.build-token$' \"$out/findings.txt\"" in refusal
    assert named(workflow, "Scan the candidate for credentials")["run"].startswith(
        'bash docker/swarm-node/scan.sh "$CANDIDATE"'
    )
    for name in (
        "Build the planted credential fixture",
        "Refuse the planted credential fixture",
        "Scan the candidate for credentials",
    ):
        assert not {"if", "continue-on-error"} & named(workflow, name).keys()


def test_the_pull_request_smoke_scans_the_image_it_built():
    smoke = (ROOT / "docker/swarm-node/smoke.sh").read_text()
    build = smoke.index('-t "$image" "$context" > "$output/build.log"')

    assert smoke.index('scan.sh" "$image" "$output/credential-scan"') > build
