import subprocess
import sys
from pathlib import Path

import yaml

LEDGER = "agentihooks-swarm-ledger"
SHARED = [
    "--set",
    "ledger.secretEnv[0].name=AGENTIHOOKS_SWARM_DATABASE_URL",
    "--set",
    "ledger.secretEnv[0].secretName=swarm-postgres",
    "--set",
    "ledger.secretEnv[0].key=url",
]
SHARED_ENV = ["--set", "ledger.env.AGENTIHOOKS_SWARM_DATABASE_URL=postgresql://ledger@db/ledger"]
EMPTY_ENV = ["--set", "ledger.env.AGENTIHOOKS_SWARM_DATABASE_URL="]
REFUSAL = "ledger.replicas above 1 needs the shared database"
EXTRA_HOST = "ledger-internal.example:8765"
WAVE = "argocd.argoproj.io/sync-wave"
HIVE_WORKLOADS = {"worker.yaml", "worker-hive.yaml"}
SERVICE_FLAGS = [
    "--set",
    "hub.enabled=true",
    "--set",
    "hub.bus.url=nats://nats.swarm:4222",
    "--set",
    "api.enabled=true",
    "--set",
    "auth.enabled=true",
    "--set",
    "githubWebhook.enabled=true",
    "--set",
    "githubWebhook.secretName=github-webhook",
    "--set",
    "hive.join.enabled=true",
    "--set",
    "hive.join.tlsSecretName=hive-join-tls",
    "--set",
    "hive.join.memberRedisUrl=rediss://redis.swarm:6379/0",
    "--set",
    "hive.join.ingressFrom[0].ipBlock.cidr=10.0.0.0/8",
]


def render(chart: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", "agentihooks-swarm", chart, *extra], capture_output=True, text=True)


def ledger_docs(text: str) -> dict:
    return {
        d["kind"]: d
        for d in yaml.safe_load_all(text)
        if d and d["metadata"]["name"] == LEDGER and d["kind"] in {"StatefulSet", "PodDisruptionBudget"}
    }


def refused(chart: str, label: str, refusal: str, *extra: str) -> list[str]:
    result = render(chart, *extra)
    if result.returncode == 0 or refusal not in result.stderr:
        return [f"{label} renders instead of failing with {refusal!r}: {result.stderr.strip()}"]
    return []


def surge_problems(chart: str, label: str, *extra: str) -> list[str]:
    result = render(chart, "--set", "ledger.replicas=2", *extra)
    if result.returncode:
        return [f"{label} fails to render two ledger replicas: {result.stderr.strip()}"]
    docs = ledger_docs(result.stdout)
    if "StatefulSet" not in docs:
        return [f"{label} renders no ledger StatefulSet named {LEDGER}"]
    found = []
    workload = docs["StatefulSet"]["spec"]
    if workload["replicas"] != 2:
        found.append(f"{label} renders {workload['replicas']} ledger replicas, expected 2")
    if workload.get("minReadySeconds", 0) < 5:
        found.append(f"{label} lets a new ledger replica count as available before it has served five seconds")
    container = workload["template"]["spec"]["containers"][0]
    pause = container.get("lifecycle", {}).get("preStop", {}).get("exec", {}).get("command", [])
    if "sleep" not in " ".join(pause):
        found.append(f"{label} stops a ledger server before the Service drops it: no preStop pause")
    if workload["template"]["spec"].get("terminationGracePeriodSeconds", 0) < 20:
        found.append(f"{label} kills a ledger server before the preStop pause and its drain end")
    budget = docs.get("PodDisruptionBudget", {}).get("spec", {})
    if budget.get("maxUnavailable") != 1:
        found.append(f"{label} renders no disruption budget that keeps all but one ledger replica up: {budget}")
    return found


def allowed_hosts(chart: str, *extra: str) -> list[str]:
    result = render(chart, *extra)
    if result.returncode:
        raise ValueError(result.stderr.strip())
    container = ledger_docs(result.stdout)["StatefulSet"]["spec"]["template"]["spec"]["containers"][0]
    return next(e["value"] for e in container["env"] if e["name"] == "SWARM_ALLOWED_HOSTS").split(",")


def host_problems(chart: str) -> list[str]:
    try:
        hosts, default = allowed_hosts(chart, "--set", f"ledger.allowedHosts={{{EXTRA_HOST}}}"), allowed_hosts(chart)
    except ValueError as exc:
        return [f"the ledger allowed hosts fail to render: {exc}"]
    if EXTRA_HOST not in hosts or f"{LEDGER}:8765" not in hosts:
        return [f"ledger.allowedHosts does not add {EXTRA_HOST} beside the service names: {hosts}"]
    if "" in default:
        return ["a chart without ledger.allowedHosts renders an empty allowed host"]
    return []


def probe_problems(chart: str) -> list[str]:
    result = render(chart)
    if result.returncode:
        return [f"the default chart fails to render: {result.stderr.strip()}"]
    container = ledger_docs(result.stdout)["StatefulSet"]["spec"]["template"]["spec"]["containers"][0]
    found = []
    for probe_name in ("readinessProbe", "livenessProbe"):
        probe = container[probe_name]
        http_get = probe.get("httpGet", {})
        headers = {h["name"]: h["value"] for h in http_get.get("httpHeaders", [])}
        if "exec" in probe:
            found.append(f"{probe_name} forks a client process instead of an httpGet check: {probe}")
        elif http_get.get("path") != "/healthz":
            found.append(f"{probe_name} does not httpGet /healthz: {probe}")
        elif headers.get("Host") != "127.0.0.1:8765":
            found.append(f"{probe_name} sends a Host header the ledger's allowed hosts refuse: {probe}")
    return found


def wave(doc: dict) -> int:
    return int((doc["metadata"].get("annotations") or {}).get(WAVE, 0))


def wave_problems(chart: str) -> list[str]:
    result = render(chart, "--set", "ledger.replicas=2", *SHARED, *SERVICE_FLAGS)
    if result.returncode:
        return [f"a release with every service Deployment fails to render: {result.stderr.strip()}"]
    ledger = ledger_docs(result.stdout).get("StatefulSet")
    if ledger is None:
        return [f"a release with every service Deployment renders no ledger StatefulSet named {LEDGER}"]
    deployments = [
        (Path(chunk.split("# Source: ", 1)[1].split("\n", 1)[0]).name, doc)
        for chunk in result.stdout.split("\n---\n")
        if (doc := yaml.safe_load(chunk)) and doc["kind"] == "Deployment"
    ]
    templates = {
        path.name for path in (Path(chart) / "templates").glob("*.yaml") if "kind: Deployment" in path.read_text()
    }
    found = [
        f"a release with every service Deployment renders none from {name}: enable it in SERVICE_FLAGS"
        for name in sorted(templates - HIVE_WORKLOADS - {source for source, _ in deployments})
    ]
    found += [
        f"{doc['metadata']['name']} from {source} rolls in sync wave {wave(doc)}, not after the ledger's wave "
        f"{wave(ledger)}, so its new pods can take the room a rolling ledger replica frees on the node its volume pins"
        for source, doc in deployments
        if wave(doc) <= wave(ledger)
    ]
    return found


def cache_problems(chart: str, size: str, *extra: str) -> list[str]:
    result = render(chart, "--set", f"ledger.cache.sizeLimit={size}", *extra)
    if result.returncode:
        return [f"ledger cache fails to render: {result.stderr.strip()}"]
    docs = ledger_docs(result.stdout)
    if "StatefulSet" not in docs:
        return [f"ledger cache renders no StatefulSet named {LEDGER}"]
    pod = docs["StatefulSet"]["spec"]["template"]["spec"]
    container = pod["containers"][0]
    environ = {entry["name"]: entry.get("value") for entry in container.get("env", [])}
    found = []
    if environ.get("AGENTIHOOKS_HOME") != "/run/agentihooks":
        found.append("ledger cache home must be /run/agentihooks")
    mount = next(
        (entry for entry in container.get("volumeMounts", []) if entry.get("mountPath") == "/run/agentihooks"), {}
    )
    if not mount:
        found.append("ledger cache home has no matching mount")
    volume = next((entry for entry in pod.get("volumes", []) if entry.get("name") == mount.get("name")), {})
    memory = volume.get("emptyDir", {})
    if memory.get("medium") != "Memory":
        found.append("ledger cache home needs a Memory emptyDir")
    if memory.get("sizeLimit") != size:
        found.append(f"ledger cache bound must be {size}")
    return found


def problems(chart: str) -> list[str]:
    found = [
        *refused(chart, "two folder ledgers", REFUSAL, "--set", "ledger.replicas=2"),
        *refused(chart, "zero ledgers", "ledger.replicas must be at least 1", "--set", "ledger.replicas=0"),
        *surge_problems(chart, "a ledger on the shared database secret", *SHARED),
        *surge_problems(chart, "a ledger on the shared database env", *SHARED_ENV),
        *refused(chart, "an empty shared database env", REFUSAL, "--set", "ledger.replicas=2", *EMPTY_ENV),
        *host_problems(chart),
        *probe_problems(chart),
        *wave_problems(chart),
        *cache_problems(chart, "512Mi"),
        *cache_problems(chart, "64Mi", "--set", "ledger.tls.secretName=ledger-tls"),
        *cache_problems(chart, "512Mi", "--set", "ledger.replicas=2", *SHARED),
    ]
    single = render(chart, *SHARED)
    if single.returncode:
        return [*found, f"one ledger on the shared database fails to render: {single.stderr.strip()}"]
    if "PodDisruptionBudget" in ledger_docs(single.stdout):
        found.append("one ledger replica renders a disruption budget that would block every node drain")
    return found


if __name__ == "__main__":
    failures = problems(sys.argv[1])
    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        sys.exit(1)
    print("the chart runs several ledger replicas only on the shared database and keeps one serving through a rollout")
