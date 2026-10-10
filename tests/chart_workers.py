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


def main(values_path: str, rendered: str) -> list[str]:
    values = yaml.safe_load(Path(values_path).read_text())["controller"]
    workers = values["workers"]
    documents = [d for d in yaml.safe_load_all(rendered) if d]
    pod = find(documents, "Deployment", "-controller")["spec"]["template"]
    container = next(c for c in pod["spec"]["containers"] if c["name"] == "controller")
    env = {e["name"]: e.get("value") for e in container["env"]}
    problems = [
        f"{name} is {env.get(name)!r}, expected {value!r}"
        for name, value in expected_env(workers).items()
        if env.get(name) != value
    ]
    if "AGENTIHOOKS_WORKER_IMAGE_DIGEST" in env or any("@sha256" in str(v) for v in env.values()):
        problems.append("the controller env pins a worker image digest")
    config_map = find(documents, "ConfigMap", "-pod-policy")
    if json.loads(config_map["data"]["pod-policy.json"]) != workers["podPolicy"]:
        problems.append("the pod policy ConfigMap differs from controller.workers.podPolicy")
    mounts = {m["name"]: m for m in container["volumeMounts"]}
    if mounts.get("pod-policy", {}).get("mountPath") != POLICY_DIR or not mounts["pod-policy"].get("readOnly"):
        problems.append("the pod policy is not mounted read only at " + POLICY_DIR)
    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    if volumes.get("pod-policy", {}).get("configMap", {}).get("name") != config_map["metadata"]["name"]:
        problems.append("the pod policy volume does not name its ConfigMap")
    if pod["spec"].get("serviceAccountName") != values["serviceAccountName"]:
        problems.append("the controller does not run as controller.serviceAccountName")
    if pod["spec"].get("automountServiceAccountToken") is not True:
        problems.append("the controller does not mount its service account token")
    if not pod["metadata"].get("annotations", {}).get("checksum/pod-policy"):
        problems.append("a pod policy change would not roll the controller")
    return problems


if __name__ == "__main__":
    found = main(sys.argv[1], sys.stdin.read())
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        sys.exit(1)
    print("the chart rendered every controller worker setting")
