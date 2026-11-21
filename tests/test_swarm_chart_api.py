import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CHART = Path(__file__).resolve().parents[1] / "deploy/helm/agentihooks-swarm"
SMOKE = CHART / "ci" / "kind-smoke.sh"


def test_api_values_default_disabled_with_its_own_resources_and_public_route() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    api = values["api"]

    assert api["enabled"] is False
    assert api["replicas"] == 1
    assert api["port"] == 8766
    assert api["secretEnv"] == []
    assert api["publicRoute"]["host"] == ""
    assert "resources" in api
    assert "podDisruptionBudget" in api


def test_the_kind_proof_renders_the_api_service_before_any_cluster_starts() -> None:
    script = SMOKE.read_text()

    assert 'python3 tests/chart_api.py "$chart"' in script
    assert script.index("python3 tests/chart_api.py") < script.index("kind create cluster")


def test_api_deployment_mirrors_the_ledgers_env_and_settings() -> None:
    template = (CHART / "templates/api.yaml").read_text()

    assert "command: [python, -m, scripts.swarm_ledger.api_service, --serve]" in template
    assert 'include "swarm.env" (dict "root" . "role" "ledger")' in template
    assert 'include "swarm.secretKeyEnv" .Values.ledger.secretEnv' in template
    assert 'include "swarm.extraEnv" .Values.ledger.env' in template
    assert 'include "swarm.secretKeyEnv" .Values.api.secretEnv' in template
    assert 'include "swarm.extraEnv" .Values.api.env' in template
    assert 'value: "0.0.0.0"' in template
    assert "LEDGER_API_PORT" in template
    assert "SWARM_PUBLIC_URL" in template
    assert "SWARM_ALLOWED_HOSTS" in template


def test_api_fails_fast_without_the_shared_database() -> None:
    template = (CHART / "templates/api.yaml").read_text()
    guard = template.split("api.replicas must be at least 1", 1)[0]

    assert "api.enabled needs the shared database" in guard
    assert "AGENTIHOOKS_SWARM_DATABASE_URL" in guard


def test_api_service_exposes_a_named_http_port() -> None:
    template = (CHART / "templates/api.yaml").read_text()

    assert "kind: Service" in template
    assert "name: {{ $host }}" in template
    assert "- name: http" in template
    assert "targetPort: http" in template


def test_api_pod_disruption_budget_is_gated_on_its_own_setting() -> None:
    template = (CHART / "templates/api.yaml").read_text()

    assert "kind: PodDisruptionBudget" in template
    assert ".Values.api.podDisruptionBudget.enabled" in template


def test_api_ingressroute_matches_write_methods_at_high_priority() -> None:
    template = (CHART / "templates/api-ingressroute.yaml").read_text()

    assert "apiVersion: traefik.io/v1alpha1" in template
    assert "kind: IngressRoute" in template
    assert ".Values.api.enabled .Values.api.publicRoute.host" in template
    assert "Method(`POST`)" in template
    assert "Method(`PUT`)" in template
    assert "Method(`PATCH`)" in template
    assert "Method(`DELETE`)" in template
    assert "Method(`OPTIONS`)" in template
    assert "priority: {{ int .Values.api.publicRoute.priority }}" in template
    assert "serversTransport: {{ . }}" in template


def test_api_ingressroute_reaches_the_api_pods_over_https_when_the_ledger_serves_tls() -> None:
    template = (CHART / "templates/api-ingressroute.yaml").read_text()
    service = template.split("port: http", 1)[1].split("serversTransport", 1)[0]

    assert "{{- if .Values.ledger.tls.secretName }}\n          scheme: https\n          {{- end }}" in service


def test_hub_and_controller_get_the_ledgers_database_url() -> None:
    hub = (CHART / "templates/hub.yaml").read_text()
    controller = (CHART / "templates/controller.yaml").read_text()

    assert 'include "swarm.databaseEnv" (dict "root" . "env" $hub.env "secretEnv" $hub.secretEnv)' in hub
    assert (
        'include "swarm.databaseEnv" (dict "root" . "env" .Values.controller.env '
        '"secretEnv" .Values.controller.secretEnv)'
    ) in controller


def test_worker_gets_the_database_url_and_worker_hive_skips_it_with_a_settings_secret() -> None:
    worker = (CHART / "templates/worker.yaml").read_text()
    worker_hive = (CHART / "templates/worker-hive.yaml").read_text()

    assert 'include "swarm.databaseEnv" (dict "root" . "env" .Values.worker.env' in worker
    assert 'include "swarm.databaseEnv"' in worker_hive
    assert "$workers.envFromSecrets" in worker_hive.split('include "swarm.databaseEnv"', 1)[1].split("\n", 1)[0]


def test_database_env_helper_never_duplicates_a_components_own_entry() -> None:
    helpers = (CHART / "templates/_helpers.tpl").read_text()

    assert '{{- define "swarm.databaseEnv" -}}' in helpers
    assert 'hasKey $env "AGENTIHOOKS_SWARM_DATABASE_URL"' in helpers
    assert 'has "AGENTIHOOKS_SWARM_DATABASE_URL" $ownNames' in helpers


def _database_url_holders() -> list[str]:
    """Return the first name part of every chart template whose container gets the shared database URL."""
    holders: set[str] = set()
    for template in (CHART / "templates").glob("*.yaml"):
        text = template.read_text()
        if 'include "swarm.databaseEnv"' in text or 'include "swarm.secretKeyEnv" .Values.ledger.secretEnv' in text:
            holders.add(template.stem.split("-", 1)[0])
    return sorted(holders)


def _ledger_secret_env_note() -> str:
    """Return the comment lines directly above the ledger's secretEnv value, lower cased and joined."""
    lines = (CHART / "values.yaml").read_text().splitlines()
    start = lines.index("ledger:")
    end = lines.index("  secretEnv: []", start)
    note: list[str] = []
    for line in reversed(lines[start:end]):
        if not line.startswith("  #"):
            break
        note.insert(0, line)
    return " ".join(note).lower()


def test_database_url_holders_are_read_from_the_templates() -> None:
    assert _database_url_holders() == ["api", "controller", "hub", "ledger", "worker"]


def test_ledger_secret_env_note_names_every_service_that_gets_the_database_url() -> None:
    note = _ledger_secret_env_note()

    assert "database url" in note
    assert [holder for holder in _database_url_holders() if holder not in note] == []


def test_service_split_names_every_holder_wherever_it_names_an_only_holder() -> None:
    text = (CHART.parents[2] / "docs/reference/service-split.md").read_text()
    blocks = [
        block.lower()
        for block in re.split(r"\n(?=- |\n)", text)
        if "AGENTIHOOKS_SWARM_DATABASE_URL" in block and re.search(r"\bonly\b", block)
    ]

    assert len(blocks) == 2
    assert [[holder for holder in _database_url_holders() if holder not in block] for block in blocks] == [[], []]


def test_api_network_policy_admits_the_same_sources_as_the_ledger() -> None:
    template = (CHART / "templates/networkpolicy.yaml").read_text()
    documents = template.split("\n---\n")
    section = next(doc for doc in documents if "{{ $fullname }}-api\n" in doc)

    assert "networkPolicy.ingressFrom" in section
    assert "port: http" in section
