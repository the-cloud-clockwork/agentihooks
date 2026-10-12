import json
import sys
from pathlib import Path

import yaml
from chart_network import peers_problems
from chart_workers import merged

RELOAD = "secret.reloader.stakater.com/reload"


def redis_env(values: dict, release: str) -> dict:
    redis = values["redis"]
    name = "AGENTIHOOKS_SWARM_REDIS_URL"
    secret = values["hive"]["join"]["redisSecret"]
    if not secret["name"]:
        secret = redis["existingSecret"]
    if secret["name"]:
        return {"name": name, "valueFrom": {"secretKeyRef": dict(secret)}}
    return {"name": name, "value": redis["url"] or f"redis://{release}-redis:6379/0"}


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


def pod_problems(pod: dict, values: dict, release: str) -> list[str]:
    join = values["hive"]["join"]
    problems = []
    [container] = pod["containers"]
    if container["image"] != f"{values['image']['repository']}:{values['image']['tag']}":
        problems.append(f"the hive join container runs {container['image']}, not the chart image")
    if container["command"] != command(join):
        problems.append(f"the hive join command is {json.dumps(container['command'])}")
    expected_env = [{"name": "AGENTIHOOKS_DEPLOYMENT", "value": values["deployment"]}, redis_env(values, release)]
    if container["env"] != expected_env:
        problems.append(f"the hive join env is {json.dumps(container['env'], sort_keys=True)}")
    if pod["volumes"] != [{"name": "tls", "secret": {"secretName": join["tlsSecretName"]}}]:
        problems.append(f"the hive join volumes are {json.dumps(pod['volumes'], sort_keys=True)}")
    if container["volumeMounts"] != [{"name": "tls", "mountPath": "/tls", "readOnly": True}]:
        problems.append("the hive join container does not mount only the TLS secret read only at /tls")
    if pod.get("automountServiceAccountToken") is not False or pod["securityContext"].get("runAsNonRoot") is not True:
        problems.append("the hive join pod mounts a service account token or may run as root")
    security = container["securityContext"]
    if security != {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}:
        problems.append(f"the hive join container security is {json.dumps(security, sort_keys=True)}")
    return problems


def main(release: str, values_paths: list[str], rendered: str) -> list[str]:
    values: dict = {}
    for path in values_paths:
        values = merged(values, yaml.safe_load(Path(path).read_text()))
    name = f"{release}-hive-join"
    documents = [d for d in yaml.safe_load_all(rendered) if d and d["metadata"]["name"] == name]
    join = values["hive"]["join"]
    if not join["enabled"]:
        return [f"the chart renders {d['kind']} {name} with hive.join off" for d in documents]
    kinds = sorted(d["kind"] for d in documents)
    if kinds != ["Deployment", "NetworkPolicy", "Service"]:
        return [f"the chart renders {kinds} for the hive join service"]
    found = {d["kind"]: d for d in documents}
    selector = {
        "app.kubernetes.io/name": "agentihooks-swarm",
        "app.kubernetes.io/instance": release,
        "app.kubernetes.io/component": "hive-join",
    }
    deployment = found["Deployment"]
    problems = pod_problems(deployment["spec"]["template"]["spec"], values, release)
    if deployment["spec"]["selector"]["matchLabels"] != selector:
        problems.append("the hive join Deployment selects other pods")
    reload = set(deployment["metadata"].get("annotations", {}).get(RELOAD, "").split(","))
    secrets = {
        join["tlsSecretName"],
        redis_env(values, release).get("valueFrom", {}).get("secretKeyRef", {}).get("name", ""),
    }
    secrets -= {""}
    if reload != secrets:
        problems.append(f"the hive join Deployment reloads on {sorted(reload)}, not {sorted(secrets)}")
    service = found["Service"]["spec"]
    if service.get("type", "ClusterIP") != "ClusterIP" or service["selector"] != selector:
        problems.append("the hive join Service is not a ClusterIP Service for the hive join pods")
    if service["ports"] != [{"name": "join", "port": join["port"], "targetPort": "join"}]:
        problems.append(f"the hive join Service ports are {json.dumps(service['ports'], sort_keys=True)}")
    policy = found["NetworkPolicy"]
    if policy["spec"]["podSelector"] != {"matchLabels": selector}:
        problems.append("the hive join policy selects other pods")
    problems += peers_problems(policy, join["ingressFrom"], "hive join")
    if policy["spec"]["ingress"][0].get("ports") != [{"port": "join"}]:
        problems.append("the hive join policy admits ports other than join")
    return problems


if __name__ == "__main__":
    found = main(sys.argv[1], sys.argv[2:], sys.stdin.read())
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        sys.exit(1)
    print("the hive join service renders as hive.join says")
