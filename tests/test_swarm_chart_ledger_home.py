import re
from pathlib import Path

import pytest
import yaml

from scripts.swarm_ledger.api.client import export_file
from scripts.swarm_ledger.repository import host_entries

pytestmark = pytest.mark.unit
CHART = Path(__file__).resolve().parents[1] / "deploy/helm/agentihooks-swarm"


@pytest.mark.feature("ledger-memory-home")
def test_ledger_keeps_its_authoritative_storage_and_ownership() -> None:
    template = (CHART / "templates/ledger.yaml").read_text()
    values = yaml.safe_load((CHART / "values.yaml").read_text())["ledger"]

    assert "- name: LEDGER_DIR\n              value: /data\n" in template
    assert "- name: data\n              mountPath: /data\n" in template
    assert "volumeClaimTemplates:\n    - metadata:\n        name: data\n" in template
    assert "accessModes: [ReadWriteOnce]" in template
    assert values["persistence"] == {"size": "1Gi", "storageClassName": ""}
    assert '- include "swarm.podSecurity" .' in template
    assert '- include "swarm.containerSecurity" .' in template
    assert "- name: ledger-tls\n              mountPath: /run/ledger-tls\n              readOnly: true\n" in template
    assert "- {key: tls.crt, path: tls.crt}" in template
    assert "- {key: tls.key, path: tls.key}" in template


@pytest.mark.feature("ledger-memory-home")
def test_ledger_entry_and_export_caches_use_the_bounded_memory_home() -> None:
    template = (CHART / "templates/ledger.yaml").read_text()
    setting = re.search(r"- name: AGENTIHOOKS_HOME\s+value: (\S+)", template)

    assert setting is not None, "ledger caches still use the container root"
    home = Path(setting[1])
    assert home == Path("/run/agentihooks")
    assert f"- name: cache\n              mountPath: {home}\n" in template
    assert (
        "- name: cache\n"
        "          emptyDir:\n"
        "            medium: Memory\n"
        "            sizeLimit: {{ .Values.ledger.cache.sizeLimit | quote }}\n"
    ) in template
    environ = {"AGENTIHOOKS_HOME": str(home)}
    assert host_entries.folder(environ) == home / "ledger-entries"
    assert export_file("http://ledger", "proof", environ).parent == home / "ledger-exports"


@pytest.mark.feature("ledger-memory-home")
def test_ledger_cache_budget_preserves_requests_and_the_process_allowance() -> None:
    ledger = yaml.safe_load((CHART / "values.yaml").read_text())["ledger"]

    assert ledger["resources"]["requests"] == {"cpu": "50m", "memory": "512Mi", "ephemeral-storage": "64Mi"}
    assert ledger["resources"]["limits"]["ephemeral-storage"] == "256Mi"
    assert ledger["cache"]["sizeLimit"] == "512Mi"
    assert ledger["resources"]["limits"]["memory"] == "2560Mi"
