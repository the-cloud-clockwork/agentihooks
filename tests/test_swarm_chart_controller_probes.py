from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.feature("controller-busy-node-probes")]

CHART = Path(__file__).resolve().parents[1] / "deploy/helm/agentihooks-swarm"


@pytest.mark.parametrize(
    ("probe", "path"),
    [("startup", "/v2/health/live"), ("liveness", "/v2/health/live"), ("readiness", "/v2/health/ready")],
)
def test_controller_api_probes_use_kubelet_http(probe: str, path: str) -> None:
    template = (CHART / "templates/controller.yaml").read_text()
    block = template.split(f"          {probe}Probe:\n", 1)[1].split("            {{- toYaml", 1)[0]

    assert "httpGet:" in block
    assert f"path: {path}" in block
    assert "port: api" in block
    assert "name: Host" in block
    assert 'value: "127.0.0.1:{{ $api.port }}"' in block
    assert "command: [agentihooks, controller," not in block


def test_controller_reserves_its_measured_cpu() -> None:
    resources = yaml.safe_load((CHART / "values.yaml").read_text())["controller"]["resources"]

    assert resources["requests"]["cpu"] == "250m"
    assert "cpu" not in resources["limits"]


def test_api_disabled_controller_keeps_its_lease_probe() -> None:
    template = (CHART / "templates/controller.yaml").read_text()

    assert "stale = [slug for slug in held if not lease.current(store, slug)]" in template


def test_kind_checks_the_rendered_http_probes() -> None:
    smoke = (CHART / "ci/kind-smoke.sh").read_text()

    assert "${probe}Probe.httpGet.path" in smoke
    assert "${probe}Probe.httpGet.httpHeaders[0].value" in smoke
    assert "${probe}Probe.exec.command" not in smoke
