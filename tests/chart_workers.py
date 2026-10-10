import json
import sys
from pathlib import Path

import yaml

POLICY_DIR = "/etc/agentihooks/pod-policy"


def expected_env(workers: dict) -> dict:
    return {
        "AGENTIHOOKS_CONTROL_API_URL": workers["apiUrl"],
        "AGENTIHOOKS_POD_POLICY_FILE": f"{POLICY_DIR}/pod-policy.json",
        "AGENTIHOOKS_WORKER_IMAGE_TAG": workers["imageTag"],
        "AGENTIHOOKS_WORKER_PROFILE": workers["profile"],
        "AGENTIHOOKS_LAUNCH_CAP": str(workers["cap"]),
        "AGENTIHOOKS_LAUNCH_PROJECTS": ",".join(workers["projects"]),
        "AGENTIHOOKS_LAUNCH_BRAIN": workers["brain"],
    }


def find(documents: list, kind: str, suffix: str) -> dict:
    [found] = [d for d in documents if d["kind"] == kind and d["metadata"]["name"].endswith(suffix)]
    return found


def merged(base: dict, layer: dict) -> dict:
    out = dict(base)
    for key, value in layer.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merged(out[key], value)
        else:
            out[key] = value
    return out


def policy_problems(documents: list, workers: dict, volume: dict) -> list[str]:
    charted = [d for d in documents if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-pod-policy")]
    if "podPolicyConfigMap" in workers:
        problems = ["the chart rendered its own pod policy beside podPolicyConfigMap"] if charted else []
        if volume.get("configMap", {}).get("name") != workers["podPolicyConfigMap"]:
            problems.append("the pod policy volume does not name controller.workers.podPolicyConfigMap")
        return problems
    if len(charted) != 1:
        return ["the chart rendered no pod policy ConfigMap for controller.workers.podPolicy"]
    [config_map] = charted
    problems = []
    if json.loads(config_map["data"]["pod-policy.json"]) != workers["podPolicy"]:
        problems.append("the pod policy ConfigMap differs from controller.workers.podPolicy")
    if volume.get("configMap", {}).get("name") != config_map["metadata"]["name"]:
        problems.append("the pod policy volume does not name its ConfigMap")
    return problems


def main(values_paths: list[str], rendered: str) -> list[str]:
    values: dict = {}
    for path in values_paths:
        values = merged(values, yaml.safe_load(Path(path).read_text()))
    values = values["controller"]
    workers = values["workers"]
    documents = [d for d in yaml.safe_load_all(rendered) if d]
    deployment = find(documents, "Deployment", "-controller")
    pod = deployment["spec"]["template"]
    container = next(c for c in pod["spec"]["containers"] if c["name"] == "controller")
    env = {e["name"]: e.get("value") for e in container["env"]}
    problems = [
        f"{name} is {env.get(name)!r}, expected {value!r}"
        for name, value in expected_env(workers).items()
        if env.get(name) != value
    ]
    if "AGENTIHOOKS_WORKER_IMAGE_DIGEST" in env or any("@sha256" in str(v) for v in env.values()):
        problems.append("the controller env pins a worker image digest")
    mounts = {m["name"]: m for m in container["volumeMounts"]}
    if mounts.get("pod-policy", {}).get("mountPath") != POLICY_DIR or not mounts["pod-policy"].get("readOnly"):
        problems.append("the pod policy is not mounted read only at " + POLICY_DIR)
    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    problems += policy_problems(documents, workers, volumes.get("pod-policy", {}))
    if pod["spec"].get("serviceAccountName") != values["serviceAccountName"]:
        problems.append("the controller does not run as controller.serviceAccountName")
    if pod["spec"].get("automountServiceAccountToken") is not True:
        problems.append("the controller does not mount its service account token")
    rolled = bool(pod["metadata"].get("annotations", {}).get("checksum/pod-policy"))
    if rolled != ("podPolicy" in workers):
        problems.append("only an inline pod policy rolls the controller through its checksum")
    reload = deployment["metadata"].get("annotations", {}).get("configmap.reloader.stakater.com/reload")
    if reload != workers.get("podPolicyConfigMap"):
        problems.append("a change to controller.workers.podPolicyConfigMap would not reload the controller")
    return problems


if __name__ == "__main__":
    found = main(sys.argv[1:], sys.stdin.read())
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        sys.exit(1)
    print("the chart rendered every controller worker setting")
