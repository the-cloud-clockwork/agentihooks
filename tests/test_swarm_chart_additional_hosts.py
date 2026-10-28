import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_public_ledger_origin_preserves_explicit_private_hosts(tmp_path):
    chart = tmp_path / "chart"
    shutil.copytree(ROOT / "deploy/helm/agentihooks-swarm", chart)
    metadata = chart / "Chart.yaml"
    document = yaml.safe_load(metadata.read_text())
    document.pop("dependencies")
    metadata.write_text(yaml.safe_dump(document))
    result = subprocess.run(
        [
            "helm",
            "template",
            "agentihooks-swarm",
            str(chart),
            "--namespace",
            "agentihooks-swarm",
            "--set",
            "nats.enabled=false",
            "--set",
            "ledger.publicUrl=https://swarm.homeofanton.com",
            "--set",
            "ledger.allowedHosts[0]=swarm-internal.homeofanton.com:8765",
            "--show-only",
            "templates/ledger.yaml",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    workload = next(item for item in yaml.safe_load_all(result.stdout) if item and item["kind"] == "StatefulSet")
    env = workload["spec"]["template"]["spec"]["containers"][0]["env"]
    hosts = [item["value"] for item in env if item["name"] == "SWARM_ALLOWED_HOSTS"]
    assert len(hosts) == 1
    assert "swarm-internal.homeofanton.com:8765" in hosts[0].split(",")
    assert "agentihooks-swarm-ledger:8765" in hosts[0].split(",")
    assert next(item["value"] for item in env if item["name"] == "SWARM_PUBLIC_URL") == (
        "https://swarm.homeofanton.com"
    )
