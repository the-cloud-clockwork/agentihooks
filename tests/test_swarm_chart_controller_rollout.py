from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy" / "helm" / "agentihooks-swarm"
SMOKE = CHART / "ci" / "kind-smoke.sh"


@pytest.mark.feature("controller-standby-replica")
def test_the_kind_proof_checks_the_controller_rollout_before_any_cluster_starts() -> None:
    script = SMOKE.read_text()

    assert 'python3 tests/chart_controller_rollout.py "$chart"' in script
    assert script.index("python3 tests/chart_controller_rollout.py") < script.index("kind create cluster")


@pytest.mark.feature("controller-standby-replica")
def test_the_chart_defaults_to_one_controller_and_rolls_more_out_with_one_always_ready() -> None:
    template = (CHART / "templates" / "controller.yaml").read_text()

    assert yaml.safe_load((CHART / "values.yaml").read_text())["controller"]["replicas"] == 1
    assert "replicas: {{ $replicas }}" in template
    assert "kind: PodDisruptionBudget" in template
    assert "maxUnavailable: 0" in template
    assert "minAvailable: 1" in template
