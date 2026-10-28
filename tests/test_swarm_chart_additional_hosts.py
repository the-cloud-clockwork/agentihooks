import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def prove_hosts(chart):
    result = subprocess.run(
        [
            "helm",
            "template",
            "agentihooks-swarm",
            chart,
            "--namespace",
            "agentihooks-swarm",
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


def test_kind_smoke_proves_private_hosts_before_creating_a_cluster():
    script = (ROOT / "deploy/helm/agentihooks-swarm/ci/kind-smoke.sh").read_text()
    command = 'python3 tests/test_swarm_chart_additional_hosts.py "$chart"'
    assert command in script
    assert script.index(command) < script.index("kind create cluster")


if __name__ == "__main__":
    prove_hosts(sys.argv[1])
    print("Public ledger origin retains private hosts in one allowed-host entry.")
