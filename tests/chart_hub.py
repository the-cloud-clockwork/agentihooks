import subprocess
import sys

import yaml

ENABLED = ["--set", "hub.enabled=true", "--set", "hub.bus.url=nats://nats.swarm:4222"]
RECEIVER = ["--set", "githubWebhook.enabled=true", "--set", "githubWebhook.secretName=github-webhook"]
RELEASE = ["--set", "image.tag=1.2.3"]
RELEASE_IMAGE = "ghcr.io/the-cloud-clockwork/agentihooks-swarm:1.2.3"
RELOAD = "secret.reloader.stakater.com/reload"


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
    volumes = {row["name"]: row for row in spec.get("volumes", [])}
    mounts = {row["name"]: row.get("mountPath") for row in container.get("volumeMounts", [])}
    if volumes.get("cache", {}).get("emptyDir") != {"medium": "Memory", "sizeLimit": "512Mi"}:
        found.append("the hub ledger caches are not on a bounded memory volume")
    if mounts.get("cache") != "/home/swarm/.agentihooks":
        found.append("the hub ledger cache volume does not cover its export and entry copy folders")
    if container.get("command") != ["agentihooks", "hub", "run"]:
        found.append(f"the hub container runs {container.get('command')}, expected agentihooks hub run")
    env = {row["name"]: row.get("value") for row in container.get("env", [])}
    bus = {"AGENTIHOOKS_BUS": "nats", "AGENTIHOOKS_BUS_URL": "nats://nats.swarm:4222"}
    if {name: env.get(name) for name in bus} != bus:
        found.append(f"the hub bus env is {[env.get(name) for name in bus]}, expected {list(bus.values())}")
    serving = env.get("AGENTIHOOKS_HUB_SERVING_FILE")
    probe = container.get("readinessProbe", {}).get("exec", {}).get("command")
    if not serving or probe != ["test", "-f", serving]:
        found.append(f"the hub turns ready on {probe} instead of its serving file {serving}")
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    if labels.get("app.kubernetes.io/component") != "hub":
        found.append("the hub pods are not labelled with the hub component")
    if spec.get("securityContext", {}).get("runAsNonRoot") is not True:
        found.append("the hub pods may run as root")
    if RELOAD in deployment["metadata"].get("annotations", {}):
        found.append("a hub without hub.secretEnv still carries a secret reload annotation")
    strategy = deployment["spec"].get("strategy", {})
    rolling = strategy.get("rollingUpdate", {})
    if strategy.get("type") != "RollingUpdate" or rolling.get("maxSurge") != 0 or rolling.get("maxUnavailable") != 1:
        found.append(f"the hub rollout adds a pod before an old one leaves: {strategy}")
    return found


def scope_problems(chart: str) -> list[str]:
    found = []
    for setting, expected in ((None, "1"), ("false", None), ("true", "1")):
        options = [] if setting is None else ["--set", f"hub.bus.scoped={setting}"]
        result = render(chart, *ENABLED, *options)
        if result.returncode:
            found.append(f"hub scope {setting} fails to render: {result.stderr.strip()}")
            continue
        deployment = hubs(result.stdout)[0]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        env = {row["name"]: row.get("value") for row in container["env"]}
        if env.get("AGENTIHOOKS_BUS_SCOPED") != expected:
            found.append(f"hub scope {setting} renders {env.get('AGENTIHOOKS_BUS_SCOPED')}, expected {expected}")
        if "AGENTIHOOKS_BUS_BIND" in env:
            found.append("the hub binds streams instead of provisioning scoped resources")
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
    reload = deployment["metadata"].get("annotations", {}).get(RELOAD)
    if reload != "github-webhook":
        found.append(f"the receiver reloads on {reload}, expected its bus credential secret github-webhook")
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
    found += hub_problems(deployments[0]) + spread_problems(chart)
    single = render(chart, *ENABLED, "--set", "hub.replicas=1")
    if single.returncode or any(d["spec"].get("strategy") for d in hubs(single.stdout)):
        found.append("a single hub replica rolls out by taking the only hub down first")
    alone = render(chart, *ENABLED, "--set", "hiveOnly=true")
    if alone.returncode or hubs(alone.stdout):
        found.append("a hive only release renders the hub")
    unaddressed = render(chart, "--set", "hub.enabled=true", "--set", "nats.enabled=false")
    if unaddressed.returncode == 0 or "hub.bus.url" not in unaddressed.stderr:
        found.append("a NATS hub without hub.bus.url renders instead of failing on the missing address")
    unmarked = render(chart, *ENABLED, "--set", "hub.servingFile=")
    if unmarked.returncode == 0 or "hub.servingFile" not in unmarked.stderr:
        found.append("a hub without hub.servingFile renders a readiness probe that never passes")
    overlaid = render(chart, *ENABLED, "--set", "hub.env.AGENTIHOOKS_HUB_SERVING_FILE=/elsewhere")
    if overlaid.returncode or any(hub_problems(d) for d in hubs(overlaid.stdout)):
        found.append("a hub.env entry for AGENTIHOOKS_HUB_SERVING_FILE moves the hub off the file its probe reads")
    if receivers(default.stdout):
        found.append("the default release renders the GitHub webhook receiver")
    return found + scope_problems(chart) + secret_env_problems(chart) + receiver_release_problems(chart)


def secret_env_problems(chart: str) -> list[str]:
    secret = ["--set", "hub.secretEnv[0].name=AGENTIHOOKS_BUS_CREDENTIALS"]
    secret += ["--set", "hub.secretEnv[0].secretName=hub-bus", "--set", "hub.secretEnv[0].key=credentials"]
    secret += ["--set", "hub.secretEnv[1].name=AGENTIHOOKS_BUS_USER", "--set", "hub.secretEnv[1].secretName=hub-bus"]
    secret += ["--set", "hub.secretEnv[1].key=user"]
    rendered = render(chart, *ENABLED, *secret)
    if rendered.returncode:
        return [f"a hub with hub.secretEnv fails to render: {rendered.stderr.strip()}"]
    deployment = hubs(rendered.stdout)[0]
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    rows = [row for row in env if row["name"] == "AGENTIHOOKS_BUS_CREDENTIALS"]
    expected = {"secretKeyRef": {"name": "hub-bus", "key": "credentials"}}
    if rows != [{"name": "AGENTIHOOKS_BUS_CREDENTIALS", "valueFrom": expected}]:
        return [f"a hub with hub.secretEnv reads its credential from {rows}, expected {expected}"]
    reload = deployment["metadata"].get("annotations", {}).get(RELOAD)
    if reload != "hub-bus":
        return [f"a hub with hub.secretEnv reloads on {reload}, expected its credential secret hub-bus once"]
    shared = ["--set", "secretEnv[0].name=AGENTIHOOKS_SHARED_TOKEN", "--set", "secretEnv[0].secretName=swarm-bus"]
    shared += ["--set", "secretEnv[0].key=token", "--set", "envFromSecrets[0]=swarm-env"]
    rendered = render(chart, *ENABLED, *secret, *shared)
    if rendered.returncode:
        return [f"a hub with shared secret env fails to render: {rendered.stderr.strip()}"]
    reload = hubs(rendered.stdout)[0]["metadata"].get("annotations", {}).get(RELOAD, "")
    expected_secrets = ["hub-bus", "swarm-bus", "swarm-env"]
    if sorted(reload.split(",")) != expected_secrets:
        return [f"a hub with shared secret env reloads on {reload}, expected each of {expected_secrets} once"]
    return []


def receiver_release_problems(chart: str) -> list[str]:
    found = []
    beside = render(chart, *ENABLED, *RECEIVER, *RELEASE)
    if beside.returncode:
        return [*found, f"an enabled receiver fails to render: {beside.stderr.strip()}"]
    found += receiver_problems(receivers(beside.stdout))
    if len(hubs(beside.stdout)) != 1:
        found.append("the receiver does not render beside the hub")
    if receiver_bus_url(beside.stdout) is not None:
        found.append("the receiver renders hub.bus.url, which overrides the authenticated bus address in its secret")
    hive = render(chart, *RECEIVER, "--set", "hiveOnly=true")
    if hive.returncode or receivers(hive.stdout):
        found.append("a hive only release renders the receiver")
    unsigned = render(chart, "--set", "githubWebhook.enabled=true")
    if unsigned.returncode == 0 or "githubWebhook.secretName" not in unsigned.stderr:
        found.append("a receiver without githubWebhook.secretName renders instead of failing on the missing secret")
    return found


def spread_problems(chart: str) -> list[str]:
    rendered = render(chart, *ENABLED)
    if rendered.returncode:
        return [f"an enabled hub fails to render: {rendered.stderr.strip()}"]
    deployment = hubs(rendered.stdout)[0]
    affinity = deployment["spec"]["template"]["spec"].get("affinity", {})
    selector = {"matchLabels": deployment["spec"]["selector"]["matchLabels"]}
    term = {"weight": 100, "podAffinityTerm": {"topologyKey": "kubernetes.io/hostname", "labelSelector": selector}}
    found = []
    if affinity.get("podAntiAffinity") != {"preferredDuringSchedulingIgnoredDuringExecution": [term]}:
        found.append(f"the hub pods do not prefer a node without another hub: {affinity.get('podAntiAffinity')}")
    if "nodeAffinity" not in affinity:
        found.append("the hub pods lose the configured node affinity beside the spread preference")
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    if not selector["matchLabels"].items() <= labels.items():
        found.append(f"the spread term selects {selector['matchLabels']}, which the hub pod labels {labels} lack")
    single = render(chart, *ENABLED, "--set", "hub.replicas=1")
    spread = [d["spec"]["template"]["spec"].get("affinity", {}).get("podAntiAffinity") for d in hubs(single.stdout)]
    if single.returncode:
        found.append(f"a single hub replica fails to render: {single.stderr.strip()}")
    elif any(spread):
        found.append(f"a single hub replica renders a spread preference: {spread}")
    key = "hub.affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[0]"
    given = ["--set", f"{key}.topologyKey=topology.kubernetes.io/zone"]
    given += ["--set", f"{key}.labelSelector.matchLabels.team=hub"]
    kept = render(chart, *ENABLED, *given)
    rows = [d["spec"]["template"]["spec"].get("affinity", {}).get("podAntiAffinity") for d in hubs(kept.stdout)]
    wanted = {"topologyKey": "topology.kubernetes.io/zone", "labelSelector": {"matchLabels": {"team": "hub"}}}
    if kept.returncode:
        found.append(f"a hub with a pod anti affinity in hub.affinity fails to render: {kept.stderr.strip()}")
    elif rows != [{"requiredDuringSchedulingIgnoredDuringExecution": [wanted]}]:
        found.append(f"a hub.affinity pod anti affinity renders as {rows}, expected it as given: {wanted}")
    return found


if __name__ == "__main__":
    failures = problems(sys.argv[1])
    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        sys.exit(1)
    print(
        "the hub renders two replicas of agentihooks hub run on the configured bus that prefer different nodes"
        " and turn ready only once they serve,"
        " and only outside a hive;"
        " the GitHub webhook receiver renders beside it on the chart image with its secret by reference"
    )
