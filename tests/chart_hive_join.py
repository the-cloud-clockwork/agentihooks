import copy
import json
import sys
from pathlib import Path

import yaml
from chart_network import peers_problems
from chart_workers import merged

RELOAD = "secret.reloader.stakater.com/reload"
SUFFIX = "-hive-join"
KINDS = ("Deployment", "Service", "NetworkPolicy")
POD_SECURITY = {
    "runAsNonRoot": True,
    "runAsUser": 10001,
    "runAsGroup": 10001,
    "fsGroup": 10001,
    "seccompProfile": {"type": "RuntimeDefault"},
}
CONTAINER_SECURITY = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
PROBE = {"tcpSocket": {"port": "join"}}


def redis_env(values: dict, fullname: str) -> dict | None:
    redis = values["redis"]
    name = "AGENTIHOOKS_SWARM_REDIS_URL"
    secret = values["hive"]["join"]["redisSecret"]
    if not secret["name"]:
        secret = redis["existingSecret"]
    if secret["name"]:
        return {"name": name, "valueFrom": {"secretKeyRef": dict(secret)}}
    if redis["url"]:
        return {"name": name, "value": redis["url"]}
    if redis["enabled"]:
        return {"name": name, "value": f"redis://{fullname}-redis:6379/0"}
    return None


def command(join: dict) -> list[str]:
    return [
        "agentihooks",
        "hive",
        "serve",
        "--host=0.0.0.0",
        f"--port={join['port']}",
        f"--redis-url={join['memberRedisUrl']}",
        "--tls-cert=/tls/tls.crt",
        "--tls-key=/tls/tls.key",
    ]


def selector(release: str) -> dict:
    return {
        "app.kubernetes.io/name": "agentihooks-swarm",
        "app.kubernetes.io/instance": release,
        "app.kubernetes.io/component": "hive-join",
    }


def chart_fullname(release: str) -> str:
    name = release if "agentihooks-swarm" in release else f"{release}-agentihooks-swarm"
    return name[:50].rstrip("-")


def container_problems(container: dict, values: dict, fullname: str) -> list[str]:
    join = values["hive"]["join"]
    problems = []
    if container["image"] != f"{values['image']['repository']}:{values['image']['tag']}":
        problems.append(f"the hive join container runs {container['image']}, not the chart image")
    if container["command"] != command(join):
        problems.append(f"the hive join command is {json.dumps(container['command'])}")
    expected_env = [{"name": "AGENTIHOOKS_DEPLOYMENT", "value": values["deployment"]}, redis_env(values, fullname)]
    if ledger_tls := values["ledger"]["tls"]["secretName"]:
        expected_env.append(
            {"name": "LEDGER_CA_PEM", "valueFrom": {"secretKeyRef": {"name": ledger_tls, "key": "ca.crt"}}}
        )
    if container.get("env") != expected_env:
        problems.append(f"the hive join env is {json.dumps(container.get('env'), sort_keys=True)}")
    if container.get("volumeMounts") != [{"name": "tls", "mountPath": "/tls", "readOnly": True}]:
        problems.append("the hive join container does not mount only the TLS secret read only at /tls")
    if container.get("securityContext") != CONTAINER_SECURITY:
        problems.append(f"the hive join container security is {json.dumps(container.get('securityContext'))}")
    if container.get("readinessProbe", {}).get("tcpSocket") != PROBE["tcpSocket"]:
        problems.append("the hive join readiness probe does not dial the join port")
    if container.get("livenessProbe", {}).get("tcpSocket") != PROBE["tcpSocket"]:
        problems.append("the hive join liveness probe does not dial the join port")
    return problems


def pod_problems(pod: dict, values: dict, fullname: str) -> list[str]:
    join = values["hive"]["join"]
    problems = []
    if len(pod["containers"]) != 1:
        problems.append(f"the hive join pod runs {len(pod['containers'])} containers, expected one")
    if pod["containers"]:
        problems += container_problems(pod["containers"][0], values, fullname)
    if pod.get("initContainers"):
        problems.append("the hive join pod runs init containers")
    if pod.get("volumes") != [{"name": "tls", "secret": {"secretName": join["tlsSecretName"]}}]:
        problems.append(f"the hive join volumes are {json.dumps(pod.get('volumes'), sort_keys=True)}")
    if pod.get("automountServiceAccountToken") is not False:
        problems.append("the hive join pod mounts a service account token")
    if pod.get("securityContext") != POD_SECURITY:
        problems.append(f"the hive join pod security is {json.dumps(pod.get('securityContext'), sort_keys=True)}")
    shared = [key for key in ("hostNetwork", "hostPID", "hostIPC") if pod.get(key)]
    if shared:
        problems.append(f"the hive join pod shares host namespace {shared}")
    if pod.get("affinity", {}) != join["affinity"]:
        problems.append(f"the hive join affinity is {json.dumps(pod.get('affinity'), sort_keys=True)}")
    if pod.get("tolerations", []) != join["tolerations"]:
        problems.append(f"the hive join tolerations are {json.dumps(pod.get('tolerations'), sort_keys=True)}")
    return problems


def deployment_problems(deployment: dict, values: dict, fullname: str, labels: dict) -> list[str]:
    join = values["hive"]["join"]
    problems = pod_problems(deployment["spec"]["template"]["spec"], values, fullname)
    if deployment["spec"]["selector"]["matchLabels"] != labels:
        problems.append("the hive join Deployment selects other pods")
    if not labels.items() <= deployment["spec"]["template"]["metadata"]["labels"].items():
        problems.append("the hive join pods lack the hive join labels")
    reload = set(deployment["metadata"].get("annotations", {}).get(RELOAD, "").split(",")) - {""}
    env = redis_env(values, fullname) or {}
    secrets = {join["tlsSecretName"], env.get("valueFrom", {}).get("secretKeyRef", {}).get("name", "")} - {""}
    if reload != secrets:
        problems.append(f"the hive join Deployment reloads on {sorted(reload)}, not {sorted(secrets)}")
    return problems


def service_problems(service: dict, join: dict, labels: dict) -> list[str]:
    spec = service["spec"]
    problems = []
    if spec.get("type", "ClusterIP") != "ClusterIP" or spec["selector"] != labels:
        problems.append("the hive join Service is not a ClusterIP Service for the hive join pods")
    if spec["ports"] != [{"name": "join", "port": join["port"], "targetPort": "join"}]:
        problems.append(f"the hive join Service ports are {json.dumps(spec['ports'], sort_keys=True)}")
    return problems


def policy_problems(policy: dict, join: dict, labels: dict) -> list[str]:
    problems = []
    if policy["spec"]["podSelector"] != {"matchLabels": labels}:
        problems.append("the hive join policy selects other pods")
    if policy["spec"].get("policyTypes") != ["Ingress"]:
        problems.append("the hive join policy does not limit ingress")
    problems += peers_problems(policy, join["ingressFrom"], "hive join")
    if any(rule.get("ports") != [{"port": "join"}] for rule in policy["spec"].get("ingress", [])):
        problems.append("the hive join policy admits ports other than join")
    return problems


def load_values(values_paths: list[str]) -> dict:
    values: dict = {}
    for path in values_paths:
        values = merged(values, yaml.safe_load(Path(path).read_text()))
    return values


def problems_in(release: str, values: dict, documents: list[dict]) -> list[str]:
    documents = [d for d in documents if d and d["metadata"]["name"].endswith(SUFFIX)]
    join = values["hive"]["join"]
    if not join["enabled"]:
        return [f"the chart renders {d['kind']} {d['metadata']['name']} with hive.join off" for d in documents]
    found = {d["kind"]: d for d in documents}
    problems = [f"the chart renders no hive join {kind}" for kind in KINDS if kind not in found]
    problems += [f"the chart renders an extra hive join {d['kind']}" for d in documents if d["kind"] not in KINDS]
    if len(documents) != len(found):
        problems.append("the chart renders a hive join kind twice")
    fullname = chart_fullname(release)
    problems += [
        f"the chart names a hive join {d['kind']} {d['metadata']['name']}, not {fullname}{SUFFIX}"
        for d in documents
        if d["metadata"]["name"] != fullname + SUFFIX
    ]
    labels = selector(release)
    if "Deployment" in found:
        problems += deployment_problems(found["Deployment"], values, fullname, labels)
    if "Service" in found:
        problems += service_problems(found["Service"], join, labels)
    if "NetworkPolicy" in found:
        problems += policy_problems(found["NetworkPolicy"], join, labels)
    return problems


def main(release: str, values_paths: list[str], rendered: str) -> list[str]:
    return problems_in(release, load_values(values_paths), list(yaml.safe_load_all(rendered)))


def _pod(docs: dict) -> dict:
    return docs["Deployment"]["spec"]["template"]["spec"]


def _container(docs: dict) -> dict:
    return _pod(docs)["containers"][0]


PLANTS = (
    ("image", lambda d: _container(d).update(image="other/image:dev"), "not the chart image"),
    ("mount", lambda d: _container(d)["volumeMounts"][0].update(readOnly=False), "does not mount only"),
    ("token", lambda d: _pod(d).update(automountServiceAccountToken=True), "service account token"),
    ("seccomp", lambda d: _pod(d)["securityContext"].pop("seccompProfile"), "the hive join pod security is"),
    ("host", lambda d: _pod(d).update(hostNetwork=True), "shares host namespace"),
    ("init", lambda d: _pod(d).update(initContainers=[{"name": "x"}]), "init containers"),
    ("sidecar", lambda d: _pod(d)["containers"].append({"name": "x"}), "containers, expected one"),
    ("privileged", lambda d: _container(d)["securityContext"].update(privileged=True), "container security"),
    ("liveness", lambda d: _container(d).pop("livenessProbe"), "liveness probe"),
    ("readiness", lambda d: _container(d).pop("readinessProbe"), "readiness probe"),
    ("affinity", lambda d: _pod(d).update(affinity={"podAffinity": {}}), "the hive join affinity is"),
    ("tolerations", lambda d: _pod(d).update(tolerations=[{"operator": "Exists"}]), "tolerations are"),
    (
        "deployment selector",
        lambda d: d["Deployment"]["spec"]["selector"]["matchLabels"].update(x="y"),
        "Deployment selects other pods",
    ),
    ("reload", lambda d: d["Deployment"]["metadata"].pop("annotations"), "reloads on"),
    ("service type", lambda d: d["Service"]["spec"].update(type="LoadBalancer"), "not a ClusterIP"),
    ("service selector", lambda d: d["Service"]["spec"]["selector"].update(x="y"), "not a ClusterIP"),
    (
        "policy selector",
        lambda d: d["NetworkPolicy"]["spec"]["podSelector"]["matchLabels"].update(x="y"),
        "policy selects other pods",
    ),
    ("policy ports", lambda d: d["NetworkPolicy"]["spec"]["ingress"][0].pop("ports"), "ports other than join"),
    ("policy types", lambda d: d["NetworkPolicy"]["spec"].pop("policyTypes"), "does not limit ingress"),
    (
        "pod labels",
        lambda d: d["Deployment"]["spec"]["template"]["metadata"]["labels"].update(
            {"app.kubernetes.io/component": "other"}
        ),
        "lack the hive join labels",
    ),
    ("no containers", lambda d: _pod(d).update(containers=[]), "runs 0 containers"),
    ("name", lambda d: d["Service"]["metadata"].update(name=f"other{SUFFIX}"), "the chart names a hive join"),
)
LIST_PLANTS = (
    ("duplicate", lambda docs: docs.append(copy.deepcopy(docs[0])), "kind twice"),
    (
        "extra",
        lambda docs: docs.append({"kind": "ConfigMap", "metadata": {"name": docs[0]["metadata"]["name"]}}),
        "an extra hive join ConfigMap",
    ),
)


def self_test(release: str, values_paths: list[str], rendered: str) -> list[str]:
    values = load_values(values_paths)
    documents = [d for d in yaml.safe_load_all(rendered) if d and d["metadata"]["name"].endswith(SUFFIX)]
    failures = [f"the clean render fails: {problem}" for problem in problems_in(release, values, documents)]
    plants = [(name, lambda docs, p=plant: p({d["kind"]: d for d in docs}), text) for name, plant, text in PLANTS]
    for name, plant, expected in [*plants, *LIST_PLANTS]:
        planted = copy.deepcopy(documents)
        plant(planted)
        if not any(expected in problem for problem in problems_in(release, values, planted)):
            failures.append(f"the chart check did not flag the planted {name} fault with {expected!r}")
    return failures


if __name__ == "__main__":
    args = sys.argv[1:]
    check = self_test if args[0] == "--self-test" else main
    args = args[1:] if check is self_test else args
    found = check(args[0], args[1:], sys.stdin.read())
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        sys.exit(1)
    if check is self_test:
        print(f"the chart check flagged all {len(PLANTS) + len(LIST_PLANTS)} planted hive join render faults")
    else:
        print("the hive join service renders as hive.join says")
