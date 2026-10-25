import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CHART = Path(__file__).resolve().parents[1] / "deploy/helm/agentihooks-swarm"


@pytest.mark.parametrize("scoped", [None, False, True])
def test_hub_scope_switch_preserves_legacy_until_migration(scoped):
    command = [
        "helm",
        "template",
        "scope",
        str(CHART),
        "--show-only",
        "templates/hub.yaml",
        "--set",
        "hub.enabled=true",
    ]
    if scoped is not None:
        command.extend(["--set", f"hub.bus.scoped={str(scoped).lower()}"])
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    hub = yaml.safe_load(result.stdout)
    container = hub["spec"]["template"]["spec"]["containers"][0]
    env = {row["name"]: row.get("value") for row in container["env"]}

    assert env.get("AGENTIHOOKS_BUS_SCOPED") == ("1" if scoped else None)
    assert env["AGENTIHOOKS_BUS"] == "nats"
    assert "AGENTIHOOKS_BUS_BIND" not in env
