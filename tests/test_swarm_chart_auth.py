from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CHART = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "agentihooks-swarm"


def test_the_auth_service_is_off_by_default_and_keeps_legacy_credentials():
    values = yaml.safe_load((CHART / "values.yaml").read_text())["auth"]

    assert values["enabled"] is False
    assert (values["replicas"], values["port"], values["legacy"]) == (2, 8768, "on")
    assert values["resources"]["requests"]["cpu"] == "20m"
    assert values["resources"]["limits"]["memory"] == "256Mi"


def test_the_auth_template_runs_the_auth_service_beside_its_service_outside_a_hive():
    template = (CHART / "templates" / "auth.yaml").read_text()

    assert template.startswith("{{- if and .Values.auth.enabled (not .Values.hiveOnly) }}")
    assert "command: [python, -m, scripts.auth, serve, --host, 0.0.0.0, --port, {{ $auth.port | quote }}]" in template
    assert "- name: AGENTIHOOKS_AUTH_LEGACY\n              value: {{ $auth.legacy | quote }}" in template
    assert '{{- include "swarm.secretKeyEnv" $auth.secretEnv | nindent 12 }}' in template
    assert template.count("path: /healthz") == 2
    assert "kind: Service" in template
    assert template.count('(dict "root" . "component" "auth")') == 2
