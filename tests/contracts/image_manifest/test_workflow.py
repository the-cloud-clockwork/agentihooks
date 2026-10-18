import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_ci_anton_runners import pool

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

    assert triggers["push"]["branches"] == ["dev"]
    assert triggers["workflow_dispatch"]["inputs"]["publish"]["type"] == "boolean"
    assert "pull_request" not in triggers


def test_proof_branches_publish_only_to_the_proof_repository(workflow):
    repository = workflow["jobs"]["image"]["env"]["REPOSITORY"]

    assert repository == (
        "${{ startsWith(github.ref, 'refs/heads/diffcheck/')"
        " && 'ghcr.io/the-cloud-clockwork/agentihooks-worker-proof'"
        " || 'ghcr.io/the-cloud-clockwork/agentihooks-worker' }}"
    )


def test_every_job_runs_on_the_isolated_anton_docker_pool_without_swarm_services(workflow):
    for job in workflow["jobs"].values():
        assert job["runs-on"] == pool("anton-oss")
        assert "uses" not in job and "services" not in job and "container" not in job
    text = WORKFLOW.read_text()
    assert not re.search(r"self-hosted|LEDGER|REDIS|AGENTIHOOKS_SWARM", text)


def test_only_the_registry_login_holds_a_credential(workflow):
    holders = [step["name"] for step in steps(workflow) if TOKEN.search(str(step))]

    assert holders == ["Log in to the registry"]
    assert named(workflow, "Log in to the registry")["with"]["password"] == "${{ secrets.GITHUB_TOKEN }}"


def test_builds_pass_only_the_source_revision(workflow):
    builds = [step["run"] for step in steps(workflow) if re.search(r"docker build\b", step.get("run", ""))]

    assert builds
    for run in builds:
        assert re.findall(r"--build-arg (\w+)", run) in (["SOURCE_REVISION"], ["WORKER_IMAGE"])


def test_qualification_precedes_login_and_publication(workflow):
    names = [step.get("name") for step in steps(workflow)]

    order = [
        "Build the candidate",
        "Build the incompatible herdr fixture",
        "Build the planted credential fixture",
        "Run every qualification and credential scan at once",
        "Require the independent requalification to match",
        "Log in to the registry",
        "Publish the qualified candidate",
    ]
    assert [name for name in names if name in order] == order


def test_publication_runs_only_on_dev_pushes_or_an_explicit_dispatch(workflow):
    gate = (
        "github.event_name == 'release'"
        " || (github.event_name == 'push' || inputs.publish)"
        " && (github.ref == 'refs/heads/dev' || startsWith(github.ref, 'refs/heads/diffcheck/'))"
    )

    assert named(workflow, "Log in to the registry")["if"] == gate
    assert named(workflow, "Publish the qualified candidate")["if"] == gate


def test_only_dev_moves_the_floating_dev_tag_to_the_published_digest(workflow):
    names = [step.get("name") for step in steps(workflow)]
    move = named(workflow, "Move the floating dev tag to the published digest")

    assert move["if"] == "(github.event_name == 'push' || inputs.publish) && github.ref == 'refs/heads/dev'"
    assert move["run"].splitlines() == [
        'digest="$(jq -r .digest "$RUNNER_TEMP/worker-image-attestation/candidate/promoted.json")"',
        'docker buildx imagetools create --tag "$REPOSITORY:dev" "$REPOSITORY@$digest"',
    ]
    assert names.index("Publish the qualified candidate") < names.index(move["name"])


def test_the_attestation_is_uploaded_even_after_a_failure(workflow):
    upload = named(workflow, "Upload the attestation")

    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "${{ runner.temp }}/worker-image-attestation"


def test_checkout_keeps_no_credential_in_the_repository(workflow):
    [checkout] = [step for step in steps(workflow) if step.get("uses", "").startswith("actions/checkout@")]

    assert checkout["with"] == {
        "fetch-depth": "${{ github.event_name == 'release' && '0' || '1' }}",
        "persist-credentials": False,
    }


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


def scan(tmp_path, status, report, args=()):
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
        ["bash", str(ROOT / "docker/swarm-node/scan.sh"), *(args or ("candidate:x", str(tmp_path / "out")))],
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
    assert log.rstrip().endswith("candidate:x")
    assert "no credentials found in candidate:x" in done.stdout


def test_scan_prefetches_the_same_pinned_scanner_it_runs(tmp_path):
    (tmp_path / "pull").mkdir()
    (tmp_path / "run").mkdir()
    pulled, pull_log = scan(tmp_path / "pull", 0, "", ("--pull-scanner",))
    done, run_log = scan(tmp_path / "run", 0, CLEAN)

    assert pulled.returncode == 0, pulled.stderr
    assert done.returncode == 0, done.stderr
    scanner = pull_log.split()[-1]
    assert pull_log.split() == ["pull", "--quiet", scanner]
    assert f" {scanner} image " in run_log


METADATA = "opt/venv/lib/python3.12/site-packages/pyjwt-2.15.1.dist-info/METADATA"


def secrets_report(*found):
    return json.dumps({"Results": [{"Target": target, "Secrets": [{"RuleID": rule}]} for rule, target in found]})


def test_scan_passes_only_a_jwt_example_in_python_package_metadata(tmp_path):
    done, _ = scan(tmp_path, 1, secrets_report(("jwt-token", METADATA)))

    assert done.returncode == 0, done.stderr
    assert (tmp_path / "out/package-examples.txt").read_text() == f"jwt-token {METADATA}\n"


@pytest.mark.parametrize(
    "found",
    [("github-app-token", METADATA), ("jwt-token", "opt/agentihooks/settings.json"), ("jwt-token", METADATA + ".bak")],
)
def test_scan_refuses_any_other_finding_beside_a_package_example(tmp_path, found):
    done, _ = scan(tmp_path, 1, secrets_report(found, ("jwt-token", METADATA)))

    assert done.returncode == 1
    assert (tmp_path / "out/findings.txt").read_text() == "{} {}\n".format(*found)


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
    proofs = named(workflow, "Run every qualification and credential scan at once")["run"]

    assert not re.search(r"gh[pousr]_[0-9A-Za-z]{36}", fixture)
    assert "'ghs_' +" in fixture and "> /opt/agentihooks/.build-token" in fixture
    assert 'bash docker/swarm-node/scan.sh "$PLANTED"' in proofs and '"$status" != 1' in proofs
    assert (
        "grep -qE '^github-app-token (.*/)?opt/agentihooks/\\.build-token$' \"$out/planted-scan/findings.txt\""
        in proofs
    )
    assert 'bash docker/swarm-node/scan.sh "$CANDIDATE" "$out/candidate-scan"' in proofs
    for name in (
        "Build the planted credential fixture",
        "Run every qualification and credential scan at once",
        "Require the independent requalification to match",
    ):
        assert not {"if", "continue-on-error"} & named(workflow, name).keys()


def test_the_pull_request_smoke_scans_every_image_it_built():
    smoke = (ROOT / "docker/swarm-node/smoke.sh").read_text()
    build = smoke.index('-t "$image" "$context" > "$output/build.log"')
    rebuild = smoke.index('-t "$rebuild" "$context" > "$output/rebuild.log"')

    assert smoke.index('scan.sh" "$image" "$output/credential-scan"') > build
    assert smoke.index('scan.sh" "$rebuild" "$output/credential-scan-rebuild"') > rebuild


def test_worker_smoke_runs_independent_proofs_in_parallel_and_compares_every_inventory():
    workflow = yaml.safe_load((ROOT / ".github/workflows/swarm-node-smoke.yml").read_text())
    image = workflow["jobs"]["worker-image"]
    assert image["strategy"]["matrix"]["proof"] == ["startup", "rebuild", "profiles"]
    assert image["strategy"]["fail-fast"] is False
    assert "needs" not in image
    run = next(step for step in image["steps"] if "smoke.sh" in step.get("run", ""))
    assert run["env"]["WORKER_PROOF"] == "${{ matrix.proof }}"
    assert run["run"].endswith(' "$WORKER_PROOF"')
    upload = next(step for step in image["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["with"]["name"] == "swarm-worker-image-${{ matrix.proof }}"
    compare = workflow["jobs"]["worker-image-compare"]
    assert compare["needs"] == ["worker-image"]
    assert compare["if"] == "always()"
    assert compare["steps"][0]["env"]["PROOF_STATUS"] == "${{ needs.worker-image.result }}"
    assert '[[ "$PROOF_STATUS" == success ]]' in compare["steps"][0]["run"]
    assert any(
        'smoke.sh "$RUNNER_TEMP/worker-image-evidence" compare "$GITHUB_SHA"' in step.get("run", "")
        for step in compare["steps"]
    )


def test_worker_smoke_shares_one_versioned_scanner_cache_across_independent_proofs():
    workflow = yaml.safe_load((ROOT / ".github/workflows/swarm-node-smoke.yml").read_text())
    image = workflow["jobs"]["worker-image"]
    cache = image["env"]["WORKER_SCANNER_CACHE"]
    assert cache == "${{ github.workspace }}/.worker-scanner-cache"
    restore = next(step for step in image["steps"] if step.get("uses") == "actions/cache/restore@v4")
    save = next(step for step in image["steps"] if step.get("uses") == "actions/cache/save@v4")
    assert restore["with"]["path"] == save["with"]["path"] == "${{ env.WORKER_SCANNER_CACHE }}"
    assert restore["with"]["key"] == save["with"]["key"]
    assert "matrix.proof" not in restore["with"]["key"]
    assert "scan.sh" in restore["with"]["key"]
    assert "versions.lock" in restore["with"]["key"]
    assert save["if"] == "success() && matrix.proof == 'startup'"
    assert restore["with"]["restore-keys"] == restore["with"]["key"].removesuffix("-${{ github.run_id }}")
    proof = next(step for step in image["steps"] if "smoke.sh" in step.get("run", ""))
    assert image["steps"].index(restore) < image["steps"].index(proof) < image["steps"].index(save)


def test_worker_smoke_reuses_the_candidate_layers_except_in_the_uncached_rebuild():
    smoke_workflow = yaml.safe_load((ROOT / ".github/workflows/swarm-node-smoke.yml").read_text())
    image_workflow = yaml.safe_load((ROOT / ".github/workflows/swarm-node-image.yml").read_text())
    steps = smoke_workflow["jobs"]["worker-image"]["steps"]
    layers = next(step for step in steps if step.get("id") == "layers")
    candidate = next(
        step for step in image_workflow["jobs"]["image"]["steps"] if step.get("name") == "Restore the candidate layers"
    )
    proof = next(step for step in steps if "smoke.sh" in step.get("run", ""))
    smoke = (ROOT / "docker/swarm-node/smoke.sh").read_text()

    assert layers["uses"] == "actions/cache/restore@v4"
    assert layers["with"] == candidate["with"]
    assert layers["if"] == "matrix.proof != 'rebuild' && runner.environment == 'self-hosted'"
    assert all(
        step["with"]["path"] == "${{ env.WORKER_SCANNER_CACHE }}"
        for step in steps
        if step.get("uses") == "actions/cache/save@v4"
    )
    assert steps.index(layers) < steps.index(proof)
    assert proof["env"]["WORKER_LAYERS"] == (
        "${{ steps.layers.outputs.cache-matched-key && format('{0}/worker-layers', runner.temp) || '' }}"
    )
    assert 'build_args+=(--cache-from "type=local,src=$WORKER_LAYERS")' in smoke
    assert smoke.index("--cache-from") < smoke.index('-t "$image" "$context" > "$output/build.log"')


def test_worker_compare_downloads_every_proof_artifact_into_its_proof_folder():
    workflow = yaml.safe_load((ROOT / ".github/workflows/swarm-node-smoke.yml").read_text())
    proofs = workflow["jobs"]["worker-image"]["strategy"]["matrix"]["proof"]
    downloads = [
        step["with"]
        for step in workflow["jobs"]["worker-image-compare"]["steps"]
        if step.get("uses", "").startswith("actions/download-artifact@")
    ]

    assert [download["name"] for download in downloads] == [f"swarm-worker-image-{proof}" for proof in proofs]
    assert [download["path"] for download in downloads] == [
        "${{ runner.temp }}/worker-image-evidence/" + proof for proof in proofs
    ]


PROOF_STUB = """#!/usr/bin/env bash
set -euo pipefail
image="$1"
out="$2"
mkdir -p "$out" "$BARRIER"
touch "$BARRIER/$(basename "$0")-$(basename "$out")"
for _ in $(seq 200); do
    [[ "$(ls "$BARRIER" | wc -l)" -ge 5 ]] && break
    sleep 0.1
done
[[ "$(ls "$BARRIER" | wc -l)" -ge 5 ]] || { echo "proofs ran one after another" >&2; exit 3; }
case "$(basename "$0") $image" in
"qualify.sh incompatible")
    echo '{"promotable": false, "report": {"worker_image_qualified_targets": 2,
        "targets": {"herdr": {"qualified": false}}}}' > "$out/attestation.json"
    exit 1;;
"scan.sh planted")
    echo "github-app-token opt/agentihooks/.build-token" > "$out/findings.txt"
    exit 1;;
esac
[[ "$FAIL" != "$(basename "$out")" ]] || { echo "planted failure in $FAIL" >&2; exit 4; }
echo '{"promotable": true, "report": {}}' > "$out/attestation.json"
"""


def run_proofs(tmp_path, workflow, fail=""):
    stubs = tmp_path / "docker/swarm-node"
    stubs.mkdir(parents=True)
    for name in ("qualify.sh", "scan.sh"):
        (stubs / name).write_text(PROOF_STUB)
    environ = os.environ | {
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "BARRIER": str(tmp_path / "barrier"),
        "FAIL": fail,
        "CANDIDATE": "candidate",
        "INCOMPATIBLE": "incompatible",
        "PLANTED": "planted",
        "GITHUB_SHA": COMMIT,
    }
    script = named(workflow, "Run every qualification and credential scan at once")["run"]
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", script],
        env=environ,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def test_every_proof_runs_at_once_and_passes_on_a_qualified_candidate(tmp_path, workflow):
    done = run_proofs(tmp_path, workflow)

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(list((tmp_path / "barrier").iterdir())) == 5
    assert done.stdout.count(" exit 0\n") == 5
    assert "::error" not in done.stdout


@pytest.mark.parametrize(("fail", "proof"), [("repeat", "requalify_candidate"), ("candidate-scan", "scan_candidate")])
def test_a_failed_proof_fails_the_step_by_name_while_every_other_proof_reports(tmp_path, workflow, fail, proof):
    done = run_proofs(tmp_path, workflow, fail)

    assert done.returncode == 1
    assert f"::error title={proof}::{proof} failed with exit 4" in done.stdout
    assert f"planted failure in {fail}" in done.stdout
    assert done.stdout.count("::group::") == 5
    assert f"failed proofs: {proof}" in done.stderr


def test_the_candidate_build_reuses_layers_saved_only_after_the_proofs(workflow):
    names = [step.get("name") for step in steps(workflow)]
    restore = named(workflow, "Restore the candidate layers")
    build = named(workflow, "Build the candidate")
    save = named(workflow, "Save the qualified candidate layers")

    assert restore["uses"] == "actions/cache/restore@v4"
    assert restore["with"]["path"] == save["with"]["path"] == "${{ runner.temp }}/worker-layers"
    assert save["uses"] == "actions/cache/save@v4"
    assert save["with"]["key"] == "${{ steps.layers.outputs.cache-primary-key }}"
    assert save["if"] == named(workflow, "Keep the qualified candidate layers")["if"]
    assert save["if"] == "steps.layers.outputs.cache-hit != 'true'"
    assert '${LAYERS_FROM:+--cache-from "$LAYERS_FROM"}' in build["run"]
    assert '${LAYERS_TO:+--cache-to "$LAYERS_TO"}' in build["run"]
    assert "mode=max" in build["env"]["LAYERS_TO"]
    assert names.index(restore["name"]) < names.index(build["name"])
    assert names.index("Require the independent requalification to match") < names.index(save["name"])


def test_the_layer_cache_is_kept_and_saved_only_after_every_publication_step(workflow):
    names = [step.get("name") for step in steps(workflow)]
    published = [
        "Publish the qualified candidate",
        "Move the floating dev tag to the published digest",
        "Tag the release version and move the release tag when it is the newest",
    ]

    for cache in ("Keep the qualified candidate layers", "Save the qualified candidate layers"):
        assert max(names.index(name) for name in published) < names.index(cache)
    assert names.index("Save the qualified candidate layers") < names.index("Upload the attestation")
