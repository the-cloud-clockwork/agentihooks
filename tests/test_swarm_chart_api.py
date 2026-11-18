import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CHART = Path(__file__).resolve().parents[1] / "deploy/helm/agentihooks-swarm"
RELEASE = "agentihooks-swarm"
API = f"{RELEASE}-api"
LEDGER = f"{RELEASE}-ledger"
HOST = "swarm.example"
ENABLED = [
    "--set",
    "api.enabled=true",
    "--set",
    "ledger.env.AGENTIHOOKS_SWARM_DATABASE_URL=postgresql://ledger@db/ledger",
]
ROUTED = [*ENABLED, "--set", f"api.publicRoute.host={HOST}"]
WRITES = ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
WRITE_MATCH = re.compile(r"Host\(`(?P<host>[^`]+)`\) && \((?P<methods>Method\(`\w+`\)(?: \|\| Method\(`\w+`\))*)\)")
REFUSAL = "api.enabled needs the shared database"
RELOAD = "secret.reloader.stakater.com/reload"


def _helm(*settings: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["helm", "template", RELEASE, str(CHART), *settings], capture_output=True, text=True)


def _documents(*settings: str) -> list[dict]:
    result = _helm(*settings)
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def _named(documents: list[dict], name: str) -> dict[str, dict]:
    return {d["kind"]: d for d in documents if (d.get("metadata") or {}).get("name") == name}


def _container(workload: dict) -> dict:
    return workload["spec"]["template"]["spec"]["containers"][0]


def _env(workload: dict) -> dict[str, str | None]:
    return {row["name"]: row.get("value") for row in _container(workload).get("env", [])}


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


def test_api_deployment_mirrors_the_ledgers_env_and_settings() -> None:
    documents = _documents(
        "--set",
        "api.enabled=true",
        "--set",
        "ledger.secretEnv[0].name=AGENTIHOOKS_SWARM_DATABASE_URL",
        "--set",
        "ledger.secretEnv[0].secretName=swarm-postgres",
        "--set",
        "ledger.secretEnv[0].key=url",
        "--set",
        "ledger.env.LEDGER_SETTING=from-ledger",
        "--set",
        "secretEnv[0].name=SHARED_TOKEN",
        "--set",
        "secretEnv[0].secretName=swarm-shared",
        "--set",
        "secretEnv[0].key=token",
        "--set",
        "envFromSecrets[0]=swarm-env",
        "--set",
        "api.env.API_SETTING=from-api",
        "--set",
        "api.secretEnv[0].name=API_TOKEN",
        "--set",
        "api.secretEnv[0].secretName=swarm-api",
        "--set",
        "api.secretEnv[0].key=token",
    )
    api = _container(_named(documents, API)["Deployment"])
    ledger = _container(_named(documents, LEDGER)["StatefulSet"])
    own = {"LEDGER_HOST", "LEDGER_PORT", "SWARM_RELOAD", "SWARM_ALLOWED_HOSTS"}

    assert api["command"] == ["python", "-m", "scripts.swarm_ledger.api_service", "--serve"]
    assert [row for row in ledger["env"] if row["name"] not in own and row not in api["env"]] == []
    assert {"name": "API_SETTING", "value": "from-api"} in api["env"]
    assert {"name": "API_TOKEN", "valueFrom": {"secretKeyRef": {"name": "swarm-api", "key": "token"}}} in api["env"]
    assert api["envFrom"] == ledger["envFrom"] == [{"secretRef": {"name": "swarm-env"}}]


def test_api_fails_fast_without_the_shared_database() -> None:
    unset = _helm("--set", "api.enabled=true", "--set", "api.replicas=0")
    empty = _helm("--set", "api.enabled=true", "--set", "ledger.env.AGENTIHOOKS_SWARM_DATABASE_URL=")
    zero = _helm(*ENABLED, "--set", "api.replicas=0")

    assert unset.returncode != 0 and REFUSAL in unset.stderr
    assert empty.returncode != 0 and REFUSAL in empty.stderr
    assert zero.returncode != 0 and "api.replicas must be at least 1" in zero.stderr


def test_api_service_exposes_a_named_http_port() -> None:
    for port in (8766, 9000):
        docs = _named(_documents(*ENABLED, "--set", f"api.port={port}"), API)
        deployment, service = docs["Deployment"], docs["Service"]
        api, env = _container(deployment), _env(deployment)
        probe = {
            "path": "/healthz",
            "port": "http",
            "scheme": "HTTP",
            "httpHeaders": [{"name": "Host", "value": f"127.0.0.1:{port}"}],
        }
        names = {f"{API}{suffix}:{port}" for suffix in ("", ".default", ".default.svc", ".default.svc.cluster.local")}

        assert service["spec"]["ports"] == [{"name": "http", "port": port, "targetPort": "http"}]
        assert service["spec"]["selector"] == deployment["spec"]["selector"]["matchLabels"]
        assert service["spec"]["selector"].items() <= deployment["spec"]["template"]["metadata"]["labels"].items()
        assert api["ports"] == [{"name": "http", "containerPort": port}]
        assert (env["LEDGER_API_HOST"], env["LEDGER_API_PORT"]) == ("0.0.0.0", str(port))
        assert api["readinessProbe"]["httpGet"] == api["livenessProbe"]["httpGet"] == probe
        assert names <= set((env["SWARM_ALLOWED_HOSTS"] or "").split(","))


def test_api_pod_disruption_budget_is_gated_on_its_own_setting() -> None:
    budget, pair = ["--set", "api.podDisruptionBudget.enabled=true"], ["--set", "api.replicas=2"]
    budgeted = _named(_documents(*ENABLED, *pair, *budget), API)

    assert "PodDisruptionBudget" not in _named(_documents(*ENABLED, *budget), API)
    assert "PodDisruptionBudget" not in _named(_documents(*ENABLED, *pair), API)
    assert budgeted["Deployment"]["spec"]["replicas"] == 2
    assert budgeted["PodDisruptionBudget"]["spec"] == {
        "maxUnavailable": 1,
        "selector": budgeted["Deployment"]["spec"]["selector"],
    }


def test_api_ingressroute_matches_write_methods_at_high_priority() -> None:
    route = _named(_documents(*ROUTED), API)["IngressRoute"]
    steering = ["--set", "api.publicRoute.priority=1500", "--set", "api.publicRoute.serversTransportName=api-transport"]
    [steered] = _named(_documents(*ROUTED, *steering), API)["IngressRoute"]["spec"]["routes"]
    [rule] = route["spec"]["routes"]
    matched = WRITE_MATCH.fullmatch(rule["match"])

    assert route["apiVersion"] == "traefik.io/v1alpha1"
    assert matched is not None, rule["match"]
    assert matched["host"] == HOST
    assert sorted(re.findall(r"`(\w+)`", matched["methods"])) == sorted(WRITES)
    assert (rule["kind"], rule["priority"], rule["services"]) == ("Rule", 1000, [{"name": API, "port": "http"}])
    assert steered["priority"] == 1500
    assert steered["services"] == [{"name": API, "port": "http", "serversTransport": "api-transport"}]
    assert "IngressRoute" not in _named(_documents(*ENABLED), API)
    assert _named(_documents("--set", f"api.publicRoute.host={HOST}"), API) == {}
    assert _named(_documents(*ROUTED, "--set", "hiveOnly=true", "--set", "nats.enabled=false"), API) == {}


def test_api_ingressroute_reaches_the_api_pods_over_https_when_the_ledger_serves_tls() -> None:
    documents = _documents(*ROUTED, "--set", "ledger.tls.secretName=ledger-tls")
    docs, ledger = _named(documents, API), _named(documents, LEDGER)
    deployment = docs["Deployment"]
    api, env = _container(deployment), _env(deployment)
    [rule] = docs["IngressRoute"]["spec"]["routes"]
    volumes = {volume["name"]: volume for volume in deployment["spec"]["template"]["spec"]["volumes"]}
    reloads = [workload["metadata"]["annotations"][RELOAD] for workload in (deployment, ledger["StatefulSet"])]

    assert rule["services"] == [{"name": API, "port": "http", "scheme": "https"}]
    assert api["readinessProbe"]["httpGet"]["scheme"] == api["livenessProbe"]["httpGet"]["scheme"] == "HTTPS"
    assert (env["LEDGER_TLS_CERT"], env["LEDGER_TLS_KEY"]) == ("/run/ledger-tls/tls.crt", "/run/ledger-tls/tls.key")
    assert {"name": "ledger-tls", "mountPath": "/run/ledger-tls", "readOnly": True} in api["volumeMounts"]
    assert volumes["ledger-tls"]["secret"]["secretName"] == "ledger-tls"
    assert reloads == ["ledger-tls", "ledger-tls"]


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


def test_api_network_policy_admits_the_same_sources_as_the_ledger() -> None:
    documents = _documents(*ENABLED, "--set", "networkPolicy.ingressFrom[0].namespaceSelector.matchLabels.team=swarm")
    api, ledger = _named(documents, API), _named(documents, LEDGER)
    policy = api["NetworkPolicy"]["spec"]
    [rule] = policy["ingress"]

    assert policy["podSelector"] == api["Deployment"]["spec"]["selector"]
    assert policy["ingress"] == ledger["NetworkPolicy"]["spec"]["ingress"]
    assert {"namespaceSelector": {"matchLabels": {"team": "swarm"}}} in rule["from"]
    assert rule["ports"] == [{"port": "http"}]
