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


@pytest.mark.feature("codex-planner-native-sandbox")
def test_native_smoke_reads_the_pinned_chart_with_scoped_secret_references() -> None:
    workflows = CHART.parents[2] / ".github/workflows"
    job = yaml.safe_load((workflows / "swarm-node-smoke.yml").read_text())["jobs"]["worker-image"]
    assert job["env"]["ANTONCORE_SANDBOX_REF"] == "7220b8a4d4ab987786daf89966d98f646182ed18"
    assert not any("runner." in str(value) for value in job["env"].values())
    render = next(step for step in job["steps"] if step.get("name") == "Render both native sandbox profiles")
    assert render["env"]["WORKER_SANDBOX_PROFILES"] == "${{ runner.temp }}/worker-image-evidence/sandbox-profiles"
    checkout = next(step for step in job["steps"] if step.get("name") == "Read the pinned sandbox chart")
    assert checkout["with"]["ref"] == "${{ env.ANTONCORE_SANDBOX_REF }}"
    assert checkout["with"]["persist-credentials"] is False
    token = next(step for step in job["steps"] if step.get("id") == "sandbox-token")
    assert token["with"]["repositories"] == "antoncore"
    assert token["with"]["permission-contents"] == "read"
    for caller in ("test.yml", "proofs.yml"):
        secrets = yaml.safe_load((workflows / caller).read_text())["jobs"]["worker-image"]["secrets"]
        assert secrets == {
            name: "${{ secrets." + name + " }}" for name in ("TCC_CI_CLIENT_ID", "TCC_CI_APP_PRIVATE_KEY")
        }
