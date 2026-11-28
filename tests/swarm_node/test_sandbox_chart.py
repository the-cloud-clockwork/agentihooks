from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
CHART = Path(__file__).resolve().parents[2] / "deploy/helm/agentihooks-swarm"


@pytest.mark.feature("codex-planner-native-sandbox")
def test_paired_profile_render_proof_is_connected() -> None:
    root = CHART.parents[2]
    proof = (root / "tests/chart_hive_rollout.py").read_text()
    smoke = (CHART / "ci/kind-smoke.sh").read_text()
    assert "python3 tests/chart_hive_rollout.py" in smoke
    for name in (
        "test_worker_sandbox_profiles_preserve_shared_security",
        "test_default_worker_sandbox_security_is_unchanged",
        "test_worker_sandbox_profiles_require_a_pair",
    ):
        assert f"def {name}() -> None:" in proof
        assert f"    {name}()" in proof


@pytest.mark.feature("codex-planner-native-sandbox")
def test_profile_defaults_and_shared_service_security_are_preserved() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert values["workers"]["sandboxProfiles"] == {"seccomp": "", "apparmor": ""}
    helpers = (CHART / "templates/_helpers.tpl").read_text()
    security = helpers.split('define "swarm.podSecurityContext"', 1)[1].split("end", 1)[0]
    assert "type: RuntimeDefault" in security
    assert "appArmorProfile" not in security
