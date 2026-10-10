import json
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
VALUES = ROOT / "deploy" / "helm" / "agentihooks-swarm" / "values.yaml"
FIXTURE = ROOT / "tests" / "fixtures" / "swarm_v2" / "control-nodes.json"


@pytest.mark.parametrize("service", ["controller", "ledger"])
def test_the_chart_places_the_service_as_the_placement_fixture_says(service):
    values = yaml.safe_load(VALUES.read_text())[service]
    placement = json.loads(FIXTURE.read_text())["placement"]

    assert {"affinity": values["affinity"], "tolerations": values["tolerations"]} == placement
