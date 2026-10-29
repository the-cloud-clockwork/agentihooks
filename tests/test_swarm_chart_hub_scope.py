from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CHART = Path(__file__).resolve().parents[1] / "deploy/helm/agentihooks-swarm"


def test_hub_scope_matches_joined_hive_members():
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    example = yaml.safe_load((CHART / "examples/hub.yaml").read_text())

    assert values["hub"]["bus"]["scoped"] is True
    assert example["hub"]["bus"]["scoped"] is True
