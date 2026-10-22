import subprocess
import sys

import yaml

ENABLED = ["--set", "hub.enabled=true", "--set", "hub.bus.url=nats://nats.swarm:4222"]


def render(chart: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", "hub", chart, *extra], capture_output=True, text=True)


def hubs(text: str) -> list[dict]:
    documents = [d for d in yaml.safe_load_all(text) if d]
    return [d for d in documents if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-hub")]


def hub_problems(deployment: dict) -> list[str]:
    found = []
    if deployment["spec"].get("replicas") != 2:
        found.append(f"the hub runs {deployment['spec'].get('replicas')} replicas, expected 2")
    spec = deployment["spec"]["template"]["spec"]
    container = spec["containers"][0]
    if container.get("command") != ["agentihooks", "hub", "run"]:
        found.append(f"the hub container runs {container.get('command')}, expected agentihooks hub run")
    env = {row["name"]: row.get("value") for row in container.get("env", [])}
    bus = {"AGENTIHOOKS_BUS": "nats", "AGENTIHOOKS_BUS_URL": "nats://nats.swarm:4222"}
    if {name: env.get(name) for name in bus} != bus:
        found.append(f"the hub bus env is {[env.get(name) for name in bus]}, expected {list(bus.values())}")
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    if labels.get("app.kubernetes.io/component") != "hub":
        found.append("the hub pods are not labelled with the hub component")
    if spec.get("securityContext", {}).get("runAsNonRoot") is not True:
        found.append("the hub pods may run as root")
    return found


def problems(chart: str) -> list[str]:
    found = []
    default = render(chart)
    if default.returncode or hubs(default.stdout):
        found.append("the default release renders a hub or fails to render")
    enabled = render(chart, *ENABLED)
    if enabled.returncode:
        return [*found, f"an enabled hub fails to render: {enabled.stderr.strip()}"]
    deployments = hubs(enabled.stdout)
    if len(deployments) != 1:
        return [*found, f"an enabled hub renders {len(deployments)} hub Deployments, expected 1"]
    found += hub_problems(deployments[0])
    alone = render(chart, *ENABLED, "--set", "hiveOnly=true")
    if alone.returncode or hubs(alone.stdout):
        found.append("a hive only release renders the hub")
    unaddressed = render(chart, "--set", "hub.enabled=true")
    if unaddressed.returncode == 0 or "hub.bus.url" not in unaddressed.stderr:
        found.append("a NATS hub without hub.bus.url renders instead of failing on the missing address")
    return found


if __name__ == "__main__":
    failures = problems(sys.argv[1])
    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        sys.exit(1)
    print("the hub renders two replicas of agentihooks hub run on the configured bus, and only outside a hive")
