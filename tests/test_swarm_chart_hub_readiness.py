from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CHART = Path(__file__).resolve().parents[1] / "deploy/helm/agentihooks-swarm"


@pytest.mark.feature("hub-ready-only-when-serving")
def test_the_hub_turns_ready_only_once_its_serving_file_exists() -> None:
    container = (CHART / "templates/hub.yaml").read_text().split("- name: hub\n", 1)[1]
    probe = "readinessProbe:\n            exec:\n              command: [test, -f, {{ $hub.servingFile | quote }}]\n"

    assert "- name: AGENTIHOOKS_HUB_SERVING_FILE\n              value: {{ $hub.servingFile | quote }}\n" in container
    assert probe in container
    serving = container.index("- name: AGENTIHOOKS_HUB_SERVING_FILE")
    assert serving > container.index('include "swarm.extraEnv" $hub.env')
    assert serving > container.index('include "swarm.secretKeyEnv" $hub.secretEnv')
    assert yaml.safe_load((CHART / "values.yaml").read_text())["hub"]["servingFile"].startswith("/tmp/")
