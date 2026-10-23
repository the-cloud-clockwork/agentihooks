from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def jobs(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())["jobs"]


def test_container_proofs_share_one_runner_without_losing_commands():
    smoke = jobs("swarm-smoke.yml")
    assert list(smoke) == ["smoke"]
    steps = smoke["smoke"]["steps"]
    commands = []
    for step in steps:
        if step.get("uses") == "docker/build-push-action@v6":
            assert step["with"]["context"] == "."
            assert step["with"]["load"] is True
            commands.append(f"docker build -t {step['with']['tags']} .")
        elif step.get("run") in {
            "docker build -t local/agentihooks-swarm:clients-check --output type=image,compression=uncompressed .",
            """docker build -t local/agentihooks-swarm:clients-check --output type=image,compression=uncompressed . &
build=$!
status=0
bash scripts/packaging/pull-runtime-image.sh public.ecr.aws/docker/library/redis:7 &&
  docker tag public.ecr.aws/docker/library/redis:7 mirror.gcr.io/library/redis:7 &
redis=$!
bash scripts/packaging/pull-runtime-image.sh public.ecr.aws/docker/library/nats:2.15.1-alpine &&
  docker tag public.ecr.aws/docker/library/nats:2.15.1-alpine mirror.gcr.io/library/nats:2.15.1-alpine &
nats=$!
bash scripts/packaging/pull-runtime-image.sh public.ecr.aws/docker/library/redis:7-alpine &&
  docker tag public.ecr.aws/docker/library/redis:7-alpine mirror.gcr.io/library/redis:7-alpine || status=$?
wait "$redis" || status=$?
wait "$nats" || status=$?
wait "$build" || status=$?
exit "$status"
""",
        }:
            commands.append("docker build -t local/agentihooks-swarm:clients-check .")
        elif "run" in step:
            commands.append(step["run"])
    assert commands == [
        "bash scripts/packaging/pull-runtime-image.sh mirror.gcr.io/library/python:3.12-slim-bookworm",
        "docker build -t local/agentihooks-swarm:clients-check .",
        "bash scripts/packaging/swarm-smoke-parallel.sh scripts/packaging/hive-launch-smoke.sh"
        " scripts/packaging/swarm-smoke.sh scripts/packaging/hive-join-smoke.sh scripts/packaging/compose-hive-smoke.sh",
        "bash scripts/packaging/swarm-init-smoke.sh",
        "bash scripts/packaging/hive-launch-attestation-proof.sh",
        "bash scripts/packaging/swarm-image-clients.sh local/agentihooks-swarm:clients-check",
    ]
    assert sum(step.get("uses") == "actions/checkout@v4" for step in steps) == 1
    for step in steps:
        if "hive-launch-attestation-proof.sh" in step.get("run", ""):
            assert step["if"] == "github.event_name == 'workflow_dispatch'"
        elif step.get("run", "").startswith("bash scripts/packaging/pull-runtime-image.sh "):
            assert "if" not in step
        elif step.get("id") == "build":
            assert "if" not in step
        elif "run" in step:
            assert step["if"] == "${{ !cancelled() && steps.build.outcome == 'success' }}"
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


def test_worker_health_reuses_the_image_the_startup_proof_kept():
    image = jobs("swarm-node-smoke.yml")["worker-image"]
    proven = image["env"]["WORKER_PROVEN_IMAGE"]
    assert "github.run_id" in proven and "github.run_attempt" in proven
    smoke = next(step for step in image["steps"] if "docker/swarm-node/smoke.sh" in step.get("run", ""))
    assert smoke["env"]["WORKER_KEEP_IMAGE"] == "${{ matrix.proof == 'startup' && env.WORKER_PROVEN_IMAGE || '' }}"
    health = next(step for step in image["steps"] if "run_health_proof.sh" in step.get("run", ""))
    assert health["env"]["WORKER_IMAGE"] == "${{ env.WORKER_PROVEN_IMAGE }}"


def test_worker_health_obtains_the_locked_base_before_building():
    steps = jobs("swarm-node-smoke.yml")["worker-image"]["steps"]
    pull = next(index for index, step in enumerate(steps) if step.get("run") == "bash docker/swarm-node/pull-base.sh")
    health = next(index for index, step in enumerate(steps) if "run_health_proof.sh" in step.get("run", ""))
    assert pull < health
    assert not steps[health].get("continue-on-error", False)
