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


def test_the_ledger_requests_steady_memory_and_preserves_its_process_budget() -> None:
    ledger = yaml.safe_load(VALUES.read_text())["ledger"]
    resources = ledger["resources"]

    assert (resources["requests"]["memory"], resources["limits"]["memory"]) == ("512Mi", "2560Mi")
    assert ledger["cache"]["sizeLimit"] == "512Mi"
