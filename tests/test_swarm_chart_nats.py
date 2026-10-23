import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy" / "helm" / "agentihooks-swarm"


def test_the_chart_pins_the_official_nats_chart_behind_its_enabled_switch():
    dependencies = yaml.safe_load((CHART / "Chart.yaml").read_text()).get("dependencies") or []
    nats = [dependency for dependency in dependencies if dependency.get("name") == "nats"]

    assert len(nats) == 1
    assert nats[0]["repository"] == "https://nats-io.github.io/k8s/helm/charts/"
    assert nats[0]["condition"] == "nats.enabled"
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(nats[0]["version"]))


def test_the_bundled_nats_runs_by_default_with_jetstream_and_the_hub_address_left_to_it():
    values = yaml.safe_load((CHART / "values.yaml").read_text())

    assert values["nats"]["enabled"] is True
    assert values["nats"]["config"]["jetstream"]["enabled"] is True
    assert values["hub"]["bus"] == {"backend": "nats", "url": ""}


def test_the_kind_proof_builds_the_dependency_before_any_render_and_checks_the_nats_switch():
    script = (CHART / "ci" / "kind-smoke.sh").read_text()
    build = script.index('helm dependency build "$chart"')

    assert build < script.index("python3 tests/chart_")
    assert build < script.index("helm lint")
    assert build < script.index("helm template")
    assert 'python3 tests/chart_nats.py "$chart"' in script


def test_the_kind_install_leaves_the_bundled_nats_out():
    values = yaml.safe_load((CHART / "ci" / "kind-values.yaml").read_text())

    assert values["nats"]["enabled"] is False
