import json
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
VALUES = ROOT / "deploy" / "helm" / "agentihooks-swarm" / "values.yaml"
FIXTURE = ROOT / "tests" / "fixtures" / "swarm_v2" / "control-nodes.json"


@pytest.mark.parametrize("service", ["controller", "ledger", "hub"])
def test_the_chart_places_the_service_as_the_placement_fixture_says(service):
    values = yaml.safe_load(VALUES.read_text())[service]
    placement = json.loads(FIXTURE.read_text())["placement"]

    assert {"affinity": values["affinity"], "tolerations": values["tolerations"]} == placement


def test_the_ledger_requests_its_steady_memory_and_is_limited_at_two_gibibytes():
    resources = yaml.safe_load(VALUES.read_text())["ledger"]["resources"]

    assert (resources["requests"]["memory"], resources["limits"]["memory"]) == ("512Mi", "2Gi")
