import re
import sys

import yaml
from chart_ledger_rollout import LEDGER, refused, render

API = "agentihooks-swarm-api"
PORT = 8766
HOST = "swarm.example"
SHARED = ["--set", "ledger.env.AGENTIHOOKS_SWARM_DATABASE_URL=postgresql://ledger@db/ledger"]
ENABLED = ["--set", "api.enabled=true", *SHARED]
ROUTED = [*ENABLED, "--set", f"api.publicRoute.host={HOST}"]
TLS = ["--set", "ledger.tls.secretName=ledger-tls"]
SOURCES = [
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
    "--set",
    "networkPolicy.ingressFrom[0].namespaceSelector.matchLabels.team=swarm",
]
API_ROWS = [
    {"name": "API_SETTING", "value": "from-api"},
    {"name": "API_TOKEN", "valueFrom": {"secretKeyRef": {"name": "swarm-api", "key": "token"}}},
]
LEDGER_ONLY = {"AGENTIHOOKS_HOME", "LEDGER_HOST", "LEDGER_PORT", "SWARM_RELOAD", "SWARM_ALLOWED_HOSTS"}
COMMAND = ["python", "-m", "scripts.swarm_ledger.api_service", "--serve"]
WRITES = ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
MATCH = re.compile(r"Host\(`(?P<host>[^`]+)`\) && \((?P<methods>Method\(`\w+`\)(?: \|\| Method\(`\w+`\))*)\)")
PLAIN = {"name": API, "port": "http"}
REFUSAL = "api.enabled needs the shared database"
RELOAD = "secret.reloader.stakater.com/reload"


def named(text: str, name: str) -> dict[str, dict]:
    return {d["kind"]: d for d in yaml.safe_load_all(text) if d and (d.get("metadata") or {}).get("name") == name}


def container(workload: dict) -> dict:
    return workload["spec"]["template"]["spec"]["containers"][0]


def env_values(workload: dict) -> dict[str, str | None]:
    return {row["name"]: row.get("value") for row in container(workload).get("env", [])}


def deployment_problems(label: str, docs: dict[str, dict], port: int, tls: bool) -> list[str]:
    if "Deployment" not in docs:
        return [f"{label} renders no Deployment named {API}"]
    deployment = docs["Deployment"]
    api, env = container(deployment), env_values(deployment)
    found = []
    if api.get("command") != COMMAND:
        found.append(f"{label} runs {api.get('command')}, expected {COMMAND}")
    if api.get("ports") != [{"name": "http", "containerPort": port}]:
        found.append(f"{label} exposes {api.get('ports')}, expected one http port {port}")
    listener = f"{env.get('LEDGER_API_HOST')}:{env.get('LEDGER_API_PORT')}"
    if listener != f"0.0.0.0:{port}":
        found.append(f"{label} listens on {listener}, expected 0.0.0.0:{port} behind its http port")
    probe = {
        "path": "/healthz",
        "port": "http",
        "scheme": "HTTPS" if tls else "HTTP",
        "httpHeaders": [{"name": "Host", "value": f"127.0.0.1:{port}"}],
    }
    for name in ("readinessProbe", "livenessProbe"):
        if (api.get(name) or {}).get("httpGet") != probe:
            found.append(f"{label} {name} is {api.get(name)}, expected an httpGet of {probe}")
    hosts = [f"{API}{suffix}:{port}" for suffix in ("", ".default", ".default.svc", ".default.svc.cluster.local")]
    allowed = (env.get("SWARM_ALLOWED_HOSTS") or "").split(",")
    if not set(hosts) <= set(allowed):
        found.append(f"{label} allows the hosts {allowed}, which miss its own Service names {hosts}")
    selector = deployment["spec"]["selector"]["matchLabels"]
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    if selector.get("app.kubernetes.io/component") != "api" or not selector.items() <= labels.items():
        found.append(f"{label} selects {selector}, which its api pods labelled {labels} do not match")
    return found


def service_problems(label: str, docs: dict[str, dict], port: int) -> list[str]:
    if "Service" not in docs or "Deployment" not in docs:
        return [f"{label} renders no Service beside its Deployment named {API}"]
    spec = docs["Service"]["spec"]
    labels = docs["Deployment"]["spec"]["template"]["metadata"]["labels"]
    selector = spec.get("selector") or {}
    found = []
    if spec.get("ports") != [{"name": "http", "port": port, "targetPort": "http"}]:
        found.append(f"{label} Service forwards {spec.get('ports')}, expected port {port} to the pods' http port")
    if selector.get("app.kubernetes.io/component") != "api" or not selector.items() <= labels.items():
        found.append(f"{label} Service selects {selector}, which the api pods labelled {labels} do not match")
    return found


def route_problems(label: str, docs: dict[str, dict], priority: int, service: dict) -> list[str]:
    if "IngressRoute" not in docs:
        return [f"{label} renders no IngressRoute named {API}"]
    routes = docs["IngressRoute"]["spec"].get("routes") or []
    if len(routes) != 1:
        return [f"{label} renders {len(routes)} routes, expected one"]
    rule = routes[0]
    matched = MATCH.fullmatch(str(rule.get("match")))
    methods = sorted(re.findall(r"`(\w+)`", matched["methods"])) if matched else []
    found = []
    if not matched or matched["host"] != HOST or methods != sorted(WRITES):
        found.append(f"{label} matches {rule.get('match')!r}, expected host {HOST} with exactly {', '.join(WRITES)}")
    if (rule.get("kind"), rule.get("priority")) != ("Rule", priority):
        found.append(
            f"{label} is a {rule.get('kind')} at priority {rule.get('priority')}, expected a Rule at {priority}"
        )
    if rule.get("services") != [service]:
        found.append(f"{label} sends writes to {rule.get('services')}, expected {[service]}")
    return found


def workload_problems(chart: str) -> list[str]:
    routed = render(chart, *ROUTED)
    if routed.returncode:
        return [f"an api with a public route fails to render: {routed.stderr.strip()}"]
    docs = named(routed.stdout, API)
    found = deployment_problems("the api", docs, PORT, False) + service_problems("the api", docs, PORT)
    found += route_problems("the api write route", docs, 1000, PLAIN)
    moved = render(chart, *ENABLED, "--set", "api.port=9000")
    if moved.returncode:
        return [*found, f"an api on port 9000 fails to render: {moved.stderr.strip()}"]
    docs = named(moved.stdout, API)
    found += deployment_problems("an api on port 9000", docs, 9000, False)
    found += service_problems("an api on port 9000", docs, 9000)
    if "IngressRoute" in docs:
        found.append("an api without api.publicRoute.host renders a write route")
    steered = render(
        chart,
        *ROUTED,
        "--set",
        "api.publicRoute.priority=1500",
        "--set",
        "api.publicRoute.serversTransportName=api-transport",
    )
    if steered.returncode:
        return [*found, f"an api route with a servers transport fails to render: {steered.stderr.strip()}"]
    service = {**PLAIN, "serversTransport": "api-transport"}
    return found + route_problems("an api route with a servers transport", named(steered.stdout, API), 1500, service)


def tls_problems(chart: str) -> list[str]:
    label = "an api beside a TLS ledger"
    result = render(chart, *ROUTED, *TLS)
    if result.returncode:
        return [f"{label} fails to render: {result.stderr.strip()}"]
    docs, ledger = named(result.stdout, API), named(result.stdout, LEDGER)
    found = deployment_problems(label, docs, PORT, True)
    found += route_problems(f"{label} write route", docs, 1000, {**PLAIN, "scheme": "https"})
    if "Deployment" not in docs or "StatefulSet" not in ledger:
        return [*found, f"{label} renders no Deployment named {API} beside a StatefulSet named {LEDGER}"]
    deployment = docs["Deployment"]
    env = env_values(deployment)
    pair = (env.get("LEDGER_TLS_CERT"), env.get("LEDGER_TLS_KEY"))
    if pair != ("/run/ledger-tls/tls.crt", "/run/ledger-tls/tls.key"):
        found.append(f"{label} serves the pair {pair}, expected the one mounted at /run/ledger-tls")
    mounts = {m["name"]: m.get("mountPath") for m in container(deployment).get("volumeMounts", [])}
    volumes = deployment["spec"]["template"]["spec"].get("volumes", [])
    secrets = {v["name"]: (v.get("secret") or {}).get("secretName") for v in volumes}
    if (mounts.get("ledger-tls"), secrets.get("ledger-tls")) != ("/run/ledger-tls", "ledger-tls"):
        found.append(f"{label} mounts {mounts} from {secrets}, expected the ledger-tls secret at /run/ledger-tls")
    reloads = [(d["metadata"].get("annotations") or {}).get(RELOAD) for d in (deployment, ledger["StatefulSet"])]
    if reloads != ["ledger-tls", "ledger-tls"]:
        found.append(f"{label} and the ledger reload on {reloads}, expected both on their TLS secret ledger-tls")
    return found


def mirror_problems(chart: str) -> list[str]:
    label = "an api beside every ledger env source"
    result = render(chart, *SOURCES)
    if result.returncode:
        return [f"{label} fails to render: {result.stderr.strip()}"]
    docs, ledger = named(result.stdout, API), named(result.stdout, LEDGER)
    missing = [f"{API} {kind}" for kind in ("Deployment", "NetworkPolicy") if kind not in docs]
    missing += [f"{LEDGER} {kind}" for kind in ("StatefulSet", "NetworkPolicy") if kind not in ledger]
    if missing:
        return [f"{label} renders no {', '.join(missing)}"]
    api, server = container(docs["Deployment"]), container(ledger["StatefulSet"])
    rows = api.get("env", [])
    found = [
        f"{label} lacks the ledger's env {row}"
        for row in server.get("env", [])
        if row["name"] not in LEDGER_ONLY and row not in rows
    ]
    found += [f"{label} lacks {row} from api.env or api.secretEnv" for row in API_ROWS if row not in rows]
    if api.get("envFrom") != server.get("envFrom"):
        found.append(f"{label} reads {api.get('envFrom')}, expected the ledger's {server.get('envFrom')}")
    policy, own = docs["NetworkPolicy"]["spec"], ledger["NetworkPolicy"]["spec"]
    if policy.get("podSelector") != docs["Deployment"]["spec"]["selector"]:
        found.append(f"{label} NetworkPolicy guards {policy.get('podSelector')}, not the api pods")
    if policy.get("ingress") != own.get("ingress"):
        found.append(
            f"{label} NetworkPolicy admits {policy.get('ingress')}, expected the ledger's {own.get('ingress')}"
        )
    return found


def budget_problems(chart: str) -> list[str]:
    found = []
    budget = ["--set", "api.podDisruptionBudget.enabled=true"]
    pair = ["--set", "api.replicas=2"]
    for label, extra, replicas, budgeted in (
        ("one api replica with a disruption budget", budget, 1, False),
        ("two api replicas without a disruption budget", pair, 2, False),
        ("two api replicas with a disruption budget", [*pair, *budget], 2, True),
    ):
        result = render(chart, *ENABLED, *extra)
        docs = {} if result.returncode else named(result.stdout, API)
        if "Deployment" not in docs:
            found.append(f"{label} renders no Deployment named {API}: {result.stderr.strip()}")
            continue
        spec = docs["Deployment"]["spec"]
        expected = {"maxUnavailable": 1, "selector": spec["selector"]} if budgeted else None
        rendered = docs.get("PodDisruptionBudget", {}).get("spec")
        if spec.get("replicas") != replicas or rendered != expected:
            found.append(
                f"{label} renders {spec.get('replicas')} replicas with the budget {rendered}, "
                f"expected {replicas} with {expected}"
            )
    return found


def problems(chart: str) -> list[str]:
    found = [
        *refused(chart, "an api without the shared database", REFUSAL, "--set", "api.enabled=true"),
        *refused(
            chart,
            "an api on an empty shared database url",
            REFUSAL,
            "--set",
            "api.enabled=true",
            "--set",
            "ledger.env.AGENTIHOOKS_SWARM_DATABASE_URL=",
        ),
        *refused(chart, "zero api replicas", "api.replicas must be at least 1", *ENABLED, "--set", "api.replicas=0"),
    ]
    for label, extra in (
        ("the default release", []),
        ("a hive only release", [*ROUTED, "--set", "hiveOnly=true", "--set", "nats.enabled=false"]),
    ):
        result = render(chart, *extra)
        if result.returncode or named(result.stdout, API):
            found.append(f"{label} renders the api service or fails to render: {result.stderr.strip()}")
    return found + workload_problems(chart) + tls_problems(chart) + mirror_problems(chart) + budget_problems(chart)


if __name__ == "__main__":
    failures = problems(sys.argv[1])
    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        sys.exit(1)
    print(
        "the api service renders only on the shared database and outside a hive, listens and answers its probes"
        " on its one http port behind its Service, carries the ledger's environment and network policy,"
        " and its public route sends only the write methods to it"
    )
