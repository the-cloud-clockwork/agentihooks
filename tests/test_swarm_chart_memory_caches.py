from pathlib import Path

import pytest
import yaml

from scripts.scheduler.service import runtime_dir
from scripts.swarm_ledger.api.client import export_file
from scripts.swarm_ledger.repository import host_entries
from scripts.tick.view import view_file

pytestmark = pytest.mark.unit
CHART = Path(__file__).resolve().parents[1] / "deploy/helm/agentihooks-swarm"


def _cache_mount(template: str) -> Path:
    mount = template.split("- name: cache\n              mountPath: ", 1)[1].splitlines()[0]
    return Path.home() / Path(mount).relative_to("/home/swarm")


@pytest.mark.feature("hub-controller-memory-caches")
def test_hub_ledger_caches_use_bounded_memory() -> None:
    template = (CHART / "templates/hub.yaml").read_text()
    assert (
        "- name: cache\n"
        "          emptyDir:\n"
        "            medium: Memory\n"
        "            sizeLimit: {{ $hub.cache.sizeLimit | quote }}\n"
    ) in template
    mount = _cache_mount(template)
    assert mount == Path.home() / ".agentihooks"
    assert export_file("http://ledger", "proof", {}).is_relative_to(mount)
    assert host_entries.folder({}).is_relative_to(mount)


@pytest.mark.feature("hub-controller-memory-caches")
def test_controller_tick_view_uses_bounded_memory() -> None:
    template = (CHART / "templates/controller.yaml").read_text()
    assert (
        "- name: cache\n"
        "          emptyDir:\n"
        "            medium: Memory\n"
        "            sizeLimit: {{ .Values.controller.cache.sizeLimit | quote }}\n"
    ) in template
    mount = _cache_mount(template)
    assert mount == Path.home() / ".cache"
    directory = runtime_dir(
        {
            "AGENTIHOOKS_HOME": "/run/agentihooks",
            "AGENTIHOOKS_STATE_BACKEND": "bus",
            "AGENTIHOOKS_BUS": "nats",
            "AGENTIHOOKS_BUS_URL": "nats://nats:4222",
        }
    )
    assert view_file("http://ledger", "proof", directory).is_relative_to(mount)


@pytest.mark.feature("hub-controller-memory-caches")
def test_cache_memory_preserves_the_process_budget() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert values["hub"]["cache"]["sizeLimit"] == "512Mi"
    assert values["controller"]["cache"]["sizeLimit"] == "512Mi"
    assert values["hub"]["resources"]["requests"]["memory"] == "384Mi"
    assert values["controller"]["resources"]["requests"]["memory"] == "384Mi"
    assert values["hub"]["resources"]["limits"]["memory"] == "1Gi"
    assert values["controller"]["resources"]["limits"]["memory"] == "1Gi"


@pytest.mark.feature("hub-controller-memory-caches")
def test_controller_keeps_its_existing_memory_home() -> None:
    template = (CHART / "templates/controller.yaml").read_text()
    assert "- name: AGENTIHOOKS_HOME\n              value: /run/agentihooks\n" in template
    assert "- name: home\n              mountPath: /run/agentihooks\n" in template
    assert "- name: home\n          emptyDir:\n            medium: Memory\n" in template
