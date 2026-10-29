from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "deploy" / "helm" / "agentihooks-swarm" / "ci" / "kind-smoke.sh"
CHART = ROOT / "deploy" / "helm" / "agentihooks-swarm"


def test_the_kind_proof_checks_the_ledger_rollout_before_any_cluster_starts():
    script = SMOKE.read_text()

    assert 'python3 tests/chart_ledger_rollout.py "$chart"' in script
    assert script.index("python3 tests/chart_ledger_rollout.py") < script.index("kind create cluster")
    assert "ledger.replicas must be 1" not in script


def test_the_ledger_answers_every_extra_host_the_values_list():
    chart = ROOT / "deploy" / "helm" / "agentihooks-swarm"
    template = (chart / "templates" / "ledger.yaml").read_text()

    assert yaml.safe_load((chart / "values.yaml").read_text())["ledger"]["allowedHosts"] == []
    assert ".Values.ledger.allowedHosts" in template


def test_the_ledger_probes_hit_the_port_directly_instead_of_forking_a_python_client():
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    healthz = helpers.split('{{- define "swarm.healthz" -}}', 1)[1].split("{{- end -}}", 1)[0]

    assert "exec" not in healthz
    assert "httpGet" in healthz and "path: /healthz" in healthz and "port: http" in healthz


def test_the_ledger_probe_sends_a_host_header_the_server_allows():
    server = (ROOT / "scripts" / "swarm_ledger" / "ledger_link.py").read_text()
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    healthz = helpers.split('{{- define "swarm.healthz" -}}', 1)[1].split("{{- end -}}", 1)[0]

    assert '"127.0.0.1"' in server.split("LOOPBACK = ", 1)[1].split("\n", 1)[0]
    assert "httpHeaders" in healthz
    assert "name: Host" in healthz
    assert "127.0.0.1:" in healthz
