import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
PROOF = ROOT / "deploy/worker/kind-proof.sh"
BASE = "ghcr.io/the-cloud-clockwork/agentihooks-worker:dev"


def builds():
    joined = re.sub(r"\\\n\s*", " ", PROOF.read_text())
    return [line for line in joined.splitlines() if "docker build" in line]


def test_kind_proof_layers_the_hive_on_the_published_worker_base():
    assert len(builds()) == 1
    (build,) = builds()
    assert "--pull" in build
    assert "-f deploy/worker/Dockerfile" in build
    assert "--build-arg WORKER_IMAGE" not in build
    assert f"ARG WORKER_IMAGE={BASE}\n" in (ROOT / "deploy/worker/Dockerfile").read_text()


def test_kind_proof_never_rebuilds_the_worker_base():
    assert "docker/swarm-node/Dockerfile" not in PROOF.read_text()
