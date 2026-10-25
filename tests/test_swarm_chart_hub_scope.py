from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CHART = Path(__file__).resolve().parents[1] / "deploy/helm/agentihooks-swarm"


def test_hub_scope_is_inactive_until_retained_work_is_migrated():
    values = yaml.safe_load((CHART / "values.yaml").read_text())

    assert values["hub"]["bus"]["scoped"] is False
