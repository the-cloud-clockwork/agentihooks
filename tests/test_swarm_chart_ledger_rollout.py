from pathlib import Path

import pytest
import yaml

from tests import chart_ledger_rollout

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "deploy" / "helm" / "agentihooks-swarm" / "ci" / "kind-smoke.sh"
CHART = ROOT / "deploy" / "helm" / "agentihooks-swarm"


def test_the_kind_proof_checks_the_ledger_rollout_before_cluster_configuration() -> None:
    script = SMOKE.read_text()

    assert 'python3 tests/chart_ledger_rollout.py "$chart"' in script
    assert script.index("python3 tests/chart_ledger_rollout.py") < script.index('wait "$cluster_setup"\n')
    assert "ledger.replicas must be 1" not in script


def test_the_ledger_answers_every_extra_host_the_values_list():
    chart = ROOT / "deploy" / "helm" / "agentihooks-swarm"
    template = (chart / "templates" / "ledger.yaml").read_text()

    assert yaml.safe_load((chart / "values.yaml").read_text())["ledger"]["allowedHosts"] == []
    assert ".Values.ledger.allowedHosts" in template


def test_the_ledger_probes_hit_the_port_directly_instead_of_forking_a_python_client():
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    healthz = helpers.split('{{- define "swarm.healthzFor" -}}', 1)[1].split("{{- end -}}", 1)[0]

    assert "exec" not in healthz
    assert "httpGet" in healthz and "path: /healthz" in healthz and "port: http" in healthz


def test_the_ledger_probe_sends_a_host_header_the_server_allows():
    server = (ROOT / "scripts" / "swarm_ledger" / "ledger_link.py").read_text()
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    healthz = helpers.split('{{- define "swarm.healthzFor" -}}', 1)[1].split("{{- end -}}", 1)[0]

    assert '"127.0.0.1"' in server.split("LOOPBACK = ", 1)[1].split("\n", 1)[0]
    assert "httpHeaders" in healthz
    assert "name: Host" in healthz
    assert "127.0.0.1:" in healthz


@pytest.mark.feature("ledger-rolls-before-deployments")
def test_every_service_deployment_rolls_in_a_sync_wave_after_the_ledger() -> None:
    texts = {path.name: path.read_text() for path in sorted((CHART / "templates").glob("*.yaml"))}
    deployments = {name for name, text in texts.items() if "kind: Deployment" in text}
    hive_workloads = {"worker.yaml", "worker-hive.yaml"}
    metadata = {
        name: texts[name].partition("kind: Deployment\nmetadata:\n")[2].partition("\nspec:\n")[0]
        for name in deployments - hive_workloads
    }

    assert hive_workloads <= deployments
    assert metadata
    assert {name for name, block in metadata.items() if 'argocd.argoproj.io/sync-wave: "1"' not in block} == set()
    assert "sync-wave" not in texts["ledger.yaml"]


@pytest.mark.feature("ledger-memory-home")
@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("none", None),
        ("missing home", "home"),
        ("wrong home", "home"),
        ("missing mount", "mount"),
        ("wrong mount", "mount"),
        ("missing volume", "Memory"),
        ("disk volume", "Memory"),
        ("missing bound", "bound"),
        ("wrong bound", "bound"),
        ("render failure", "render"),
        ("missing ledger", "StatefulSet"),
    ],
)
def test_rendered_ledger_home_guard_rejects_each_storage_fault(
    monkeypatch: pytest.MonkeyPatch, fault: str, expected: str | None
) -> None:
    container = {
        "env": [{"name": "AGENTIHOOKS_HOME", "value": "/run/agentihooks"}],
        "volumeMounts": [{"name": "cache", "mountPath": "/run/agentihooks"}],
    }
    pod = {
        "containers": [container],
        "volumes": [{"name": "cache", "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}}],
    }
    if fault == "missing home":
        container["env"] = []
    elif fault == "wrong home":
        container["env"][0]["value"] = "/home/swarm/.agentihooks"
    elif fault == "missing mount":
        container["volumeMounts"] = []
    elif fault == "wrong mount":
        container["volumeMounts"][0]["mountPath"] = "/data"
    elif fault == "missing volume":
        pod["volumes"] = []
    elif fault == "disk volume":
        pod["volumes"][0]["emptyDir"]["medium"] = ""
    elif fault == "missing bound":
        del pod["volumes"][0]["emptyDir"]["sizeLimit"]
    elif fault == "wrong bound":
        pod["volumes"][0]["emptyDir"]["sizeLimit"] = "512Mi"
    workload = {
        "kind": "StatefulSet",
        "metadata": {"name": chart_ledger_rollout.LEDGER},
        "spec": {"template": {"spec": pod}},
    }

    def rendered(*args: str) -> chart_ledger_rollout.subprocess.CompletedProcess[str]:
        assert args == ("chart", "--set", "ledger.cache.sizeLimit=64Mi")
        return chart_ledger_rollout.subprocess.CompletedProcess(
            args,
            int(fault == "render failure"),
            "" if fault == "missing ledger" else yaml.safe_dump(workload),
            "bad template" if fault == "render failure" else "",
        )

    monkeypatch.setattr(chart_ledger_rollout, "render", rendered)
    found = chart_ledger_rollout.cache_problems("chart", "64Mi")

    if expected is None:
        assert found == []
    else:
        assert any(expected in problem for problem in found)
