import subprocess
import sys

import yaml

HUB = ["--set", "hub.enabled=true"]
EXTERNAL = ["--set", "nats.enabled=false", "--set", "hub.bus.url=nats://bus.example:4222"]


def render(chart: str, release: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", release, chart, *extra], capture_output=True, text=True)


def documents(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text) if d]


def nats(docs: list[dict]) -> list[dict]:
    return [d for d in docs if d["metadata"].get("labels", {}).get("helm.sh/chart", "").startswith("nats-")]


def bus_url(docs: list[dict]) -> str | None:
    for d in docs:
        if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-hub"):
            env = {row["name"]: row.get("value") for row in d["spec"]["template"]["spec"]["containers"][0]["env"]}
            return env.get("AGENTIHOOKS_BUS_URL")
    return None


def bundled_problems(chart: str, release: str, service: str, *extra: str) -> list[str]:
    result = render(chart, release, *HUB, *extra)
    if result.returncode:
        return [f"release {release} with the bundled NATS fails to render: {result.stderr.strip()}"]
    docs = nats(documents(result.stdout))
    found = []
    if not any(d["kind"] == "StatefulSet" for d in docs):
        found.append(f"release {release} renders no NATS StatefulSet by default")
    services = {d["metadata"]["name"] for d in docs if d["kind"] == "Service"}
    if service not in services:
        found.append(f"release {release} renders NATS Services {sorted(services)}, expected {service}")
    url = bus_url(documents(result.stdout))
    if url != f"nats://{service}:4222":
        found.append(f"release {release} points the hub at {url}, expected nats://{service}:4222")
    if not any(d["kind"] == "ConfigMap" and "jetstream" in str(d.get("data")) for d in docs):
        found.append(f"release {release} renders the bundled NATS without JetStream")
    return found


def problems(chart: str) -> list[str]:
    found = [
        *bundled_problems(chart, "swarm", "swarm-nats"),
        *bundled_problems(chart, "nats", "nats"),
        *bundled_problems(chart, "swarm", "swarm-bus", "--set", "nats.nameOverride=bus"),
        *bundled_problems(chart, "swarm", "bus", "--set", "nats.fullnameOverride=bus"),
    ]
    external = render(chart, "swarm", *HUB, *EXTERNAL)
    if external.returncode:
        return [*found, f"an external NATS fails to render: {external.stderr.strip()}"]
    docs = documents(external.stdout)
    if nats(docs):
        found.append("an external NATS release still renders the bundled NATS")
    if bus_url(docs) != "nats://bus.example:4222":
        found.append(f"an external NATS release points the hub at {bus_url(docs)}, expected nats://bus.example:4222")
    unaddressed = render(chart, "swarm", *HUB, "--set", "nats.enabled=false")
    if unaddressed.returncode == 0 or "hub.bus.url" not in unaddressed.stderr:
        found.append("a hub with neither the bundled NATS nor hub.bus.url renders instead of failing")
    return found


if __name__ == "__main__":
    failures = problems(sys.argv[1])
    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        sys.exit(1)
    print("the chart bundles the pinned NATS by default and points the hub at it, or at an external NATS instead")
