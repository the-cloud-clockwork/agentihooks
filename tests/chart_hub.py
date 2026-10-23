import subprocess
import sys

import yaml

ENABLED = ["--set", "hub.enabled=true", "--set", "hub.bus.url=nats://nats.swarm:4222"]
RECEIVER = ["--set", "githubWebhook.enabled=true", "--set", "githubWebhook.secretName=github-webhook"]
RELEASE = ["--set", "image.tag=1.2.3"]
RELEASE_IMAGE = "ghcr.io/the-cloud-clockwork/agentihooks-swarm:1.2.3"


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


def receivers(text: str) -> list[dict]:
    documents = [d for d in yaml.safe_load_all(text) if d]
    return [d for d in documents if d["metadata"]["name"].endswith("-github-webhook")]


def receiver_bus_url(text: str) -> str | None:
    deployments = [d for d in receivers(text) if d["kind"] == "Deployment"]
    env = deployments[0]["spec"]["template"]["spec"]["containers"][0].get("env", []) if deployments else []
    return next((row.get("value") for row in env if row["name"] == "AGENTIHOOKS_BUS_URL"), None)


def receiver_problems(documents: list[dict]) -> list[str]:
    kinds = sorted(d["kind"] for d in documents)
    if kinds != ["Deployment", "Service"]:
        return [f"an enabled receiver renders {kinds}, expected one Deployment and one Service"]
    deployment, service = sorted(documents, key=lambda d: d["kind"])
    found = []
    spec = deployment["spec"]["template"]["spec"]
    container = spec["containers"][0]
    if container.get("image") != RELEASE_IMAGE:
        found.append(f"the receiver runs {container.get('image')}, expected the chart image {RELEASE_IMAGE}")
    command = ["agentihooks", "github-webhook", "--host", "0.0.0.0", "--port", "8790"]
    if container.get("command") != command:
        found.append(f"the receiver container runs {container.get('command')}, expected {command}")
    if container.get("envFrom") != [{"secretRef": {"name": "github-webhook"}}]:
        found.append(f"the receiver reads {container.get('envFrom')}, expected its webhook secret by reference")
    env = {row["name"]: row.get("value") for row in container.get("env", [])}
    if env.get("AGENTIHOOKS_BUS") != "nats":
        found.append(f"the receiver bus is {env.get('AGENTIHOOKS_BUS')}, expected nats")
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    if labels.get("app.kubernetes.io/component") != "github-webhook":
        found.append("the receiver pods are not labelled with the github-webhook component")
    if spec.get("securityContext", {}).get("runAsNonRoot") is not True:
        found.append("the receiver pods may run as root")
    ports = [(p.get("port"), p.get("targetPort")) for p in service["spec"]["ports"]]
    if ports != [(8790, "http")] or service["spec"]["selector"].get("app.kubernetes.io/component") != "github-webhook":
        found.append(
            f"the receiver Service forwards {ports} to {service['spec']['selector']}, expected 8790 to its pods"
        )
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
    unaddressed = render(chart, "--set", "hub.enabled=true", "--set", "nats.enabled=false")
    if unaddressed.returncode == 0 or "hub.bus.url" not in unaddressed.stderr:
        found.append("a NATS hub without hub.bus.url renders instead of failing on the missing address")
    if receivers(default.stdout):
        found.append("the default release renders the GitHub webhook receiver")
    return found + receiver_release_problems(chart)


def receiver_release_problems(chart: str) -> list[str]:
    found = []
    beside = render(chart, *ENABLED, *RECEIVER, *RELEASE)
    if beside.returncode:
        return [*found, f"an enabled receiver fails to render: {beside.stderr.strip()}"]
    found += receiver_problems(receivers(beside.stdout))
    if len(hubs(beside.stdout)) != 1:
        found.append("the receiver does not render beside the hub")
    if receiver_bus_url(beside.stdout) != "nats://nats.swarm:4222":
        found.append(f"the receiver bus address is {receiver_bus_url(beside.stdout)}, expected hub.bus.url")
    unset = render(chart, *RECEIVER)
    if unset.returncode or receiver_bus_url(unset.stdout) is not None:
        found.append("a receiver without hub.bus.url renders a bus address that overrides its secret")
    hive = render(chart, *RECEIVER, "--set", "hiveOnly=true")
    if hive.returncode or receivers(hive.stdout):
        found.append("a hive only release renders the receiver")
    unsigned = render(chart, "--set", "githubWebhook.enabled=true")
    if unsigned.returncode == 0 or "githubWebhook.secretName" not in unsigned.stderr:
        found.append("a receiver without githubWebhook.secretName renders instead of failing on the missing secret")
    return found


if __name__ == "__main__":
    failures = problems(sys.argv[1])
    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        sys.exit(1)
    print(
        "the hub renders two replicas of agentihooks hub run on the configured bus, and only outside a hive;"
        " the GitHub webhook receiver renders beside it on the chart image with its secret by reference"
    )
