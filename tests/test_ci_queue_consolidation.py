from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def jobs(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())["jobs"]


def test_container_proofs_share_one_runner_without_losing_commands():
    smoke = jobs("swarm-smoke.yml")
    assert list(smoke) == ["smoke"]
    steps = smoke["smoke"]["steps"]
    assert [step["run"] for step in steps if "run" in step] == [
        "bash scripts/packaging/hive-launch-smoke.sh",
        "bash scripts/packaging/hive-launch-attestation-proof.sh",
        "bash scripts/packaging/swarm-smoke.sh",
        "bash scripts/packaging/hive-join-smoke.sh",
        "bash scripts/packaging/compose-hive-smoke.sh",
        "docker build -t local/agentihooks-swarm:clients-check .",
        "bash scripts/packaging/swarm-image-clients.sh local/agentihooks-swarm:clients-check",
    ]
    assert sum(step.get("uses") == "actions/checkout@v4" for step in steps) == 1
    for step in steps:
        if "hive-launch-attestation-proof.sh" in step.get("run", ""):
            assert step["if"] == "github.event_name == 'workflow_dispatch'"
        elif "run" in step:
            assert step["if"] == "${{ !cancelled() }}"
        assert not step.get("continue-on-error", False)


def test_worker_health_shares_startup_runner_and_preserves_independent_proofs():
    worker = jobs("swarm-node-smoke.yml")
    assert set(worker) == {"worker-image", "worker-image-compare"}
    image = worker["worker-image"]
    assert image["strategy"] == {"fail-fast": False, "matrix": {"proof": ["startup", "rebuild", "profiles"]}}
    health = next(step for step in image["steps"] if "run_health_proof.sh" in step.get("run", ""))
    assert health["if"] == "${{ !cancelled() && matrix.proof == 'startup' }}"
    upload = next(step for step in image["steps"] if step.get("with", {}).get("name") == "swarm-worker-health-evidence")
    assert upload["if"] == "${{ always() && matrix.proof == 'startup' }}"
    assert upload["with"]["path"] == "${{ runner.temp }}/worker-health-evidence"
    assert worker["worker-image-compare"]["needs"] == ["worker-image"]


@pytest.mark.parametrize(
    "script",
    [
        "hive-launch-smoke.sh",
        "hive-launch-attestation-proof.sh",
        "swarm-smoke.sh",
        "hive-join-smoke.sh",
        "compose-hive-smoke.sh",
        "swarm-image-clients.sh",
    ],
)
def test_each_container_proof_remains_wired(script):
    steps = jobs("swarm-smoke.yml")["smoke"]["steps"]
    assert sum(script in step.get("run", "") for step in steps) == 1


def test_worker_health_obtains_the_locked_base_before_building():
    steps = jobs("swarm-node-smoke.yml")["worker-image"]["steps"]
    pull = next(index for index, step in enumerate(steps) if step.get("run") == "bash docker/swarm-node/pull-base.sh")
    health = next(index for index, step in enumerate(steps) if "run_health_proof.sh" in step.get("run", ""))
    assert pull < health
    assert not steps[health].get("continue-on-error", False)


def test_queue_baseline_keeps_its_light_runner():
    assert "&& 'anton-oss-light' || 'ubuntu-latest'" in jobs("test.yml")["queue-baseline"]["runs-on"]
