import subprocess
import sys

import yaml

CONTROLLER = "agentihooks-swarm-controller"
REPLICA_ENV = "AGENTIHOOKS_CONTROLLER_REPLICA"
API = [
    "--set",
    "controller.api.enabled=true",
    "--set",
    "controller.api.swarm=rig",
    "--set",
    "controller.api.signingKey.keyId=kind-launch",
    "--set",
    "controller.api.signingKey.secretName=swarm-launch-signing",
]


def render(chart: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", "agentihooks-swarm", chart, *extra], capture_output=True, text=True)


def controller_docs(text: str) -> dict:
    return {
        d["kind"]: d
        for d in yaml.safe_load_all(text)
        if d and d["metadata"]["name"] == CONTROLLER and d["kind"] in {"Deployment", "PodDisruptionBudget"}
    }


def container(docs: dict) -> dict:
    return next(c for c in docs["Deployment"]["spec"]["template"]["spec"]["containers"] if c["name"] == "controller")


def env_sources(found: dict) -> dict:
    return {e["name"]: e.get("valueFrom", {}).get("fieldRef", {}).get("fieldPath") for e in found.get("env", [])}


def replica_problems(chart: str, label: str, *extra: str) -> list[str]:
    result = render(chart, *extra)
    if result.returncode:
        return [f"{label} fails to render: {result.stderr.strip()}"]
    docs = controller_docs(result.stdout)
    if "Deployment" not in docs:
        return [f"{label} renders no controller Deployment named {CONTROLLER}"]
    found = []
    controller = container(docs)
    sources = env_sources(controller)
    if sources.get(REPLICA_ENV) != "metadata.name":
        found.append(f"{label} does not name each controller replica after its Pod: {sources.get(REPLICA_ENV)}")
    if sources.get("AGENTIHOOKS_POD_IP") != "status.podIP":
        found.append(f"{label} gives the controller no Pod address to publish to its standby")
    command = " ".join(controller["command"])
    if f'agentihooks hive controller --holder "${REPLICA_ENV}"' not in command:
        found.append(f"{label} issues one shared controller credential that a second replica retires: {command}")
    return found


def surge_problems(chart: str, label: str, *extra: str) -> list[str]:
    result = render(chart, "--set", "controller.replicas=2", *extra)
    if result.returncode:
        return [f"{label} fails to render two controller replicas: {result.stderr.strip()}"]
    docs = controller_docs(result.stdout)
    spec = docs["Deployment"]["spec"]
    found = []
    if spec["replicas"] != 2:
        found.append(f"{label} renders {spec['replicas']} controller replicas, expected 2")
    strategy = spec.get("strategy", {})
    rolling = strategy.get("rollingUpdate", {})
    if strategy.get("type") != "RollingUpdate" or rolling.get("maxUnavailable") != 0 or rolling.get("maxSurge") != 1:
        found.append(f"{label} can roll out with no ready controller: {strategy}")
    budget = docs.get("PodDisruptionBudget", {}).get("spec", {})
    if budget.get("minAvailable") != 1:
        found.append(f"{label} renders no disruption budget that keeps one controller up: {budget}")
    return found


def problems(chart: str) -> list[str]:
    found = [
        *replica_problems(chart, "the default controller"),
        *replica_problems(chart, "a controller serving the API", *API),
        *surge_problems(chart, "two controllers"),
        *surge_problems(chart, "two controllers serving the API", *API),
    ]
    refusal = render(chart, "--set", "controller.replicas=0")
    if refusal.returncode == 0 or "controller.replicas must be at least 1" not in refusal.stderr:
        found.append(f"zero controllers render instead of failing: {refusal.stderr.strip()}")
    single = render(chart)
    if single.returncode:
        return [*found, f"one controller fails to render: {single.stderr.strip()}"]
    docs = controller_docs(single.stdout)
    if docs["Deployment"]["spec"].get("strategy", {}).get("type") != "Recreate":
        found.append("one controller replica no longer rolls out by Recreate")
    if "PodDisruptionBudget" in docs:
        found.append("one controller replica renders a disruption budget that would block every node drain")
    return found


if __name__ == "__main__":
    failures = problems(sys.argv[1])
    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        sys.exit(1)
    print("the chart runs a standby controller replica and keeps one controller ready through a rollout")
