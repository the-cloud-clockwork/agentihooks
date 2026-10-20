import subprocess
import sys

import yaml

BASE = ["--set", "workers.replicas=1", "--set", "workers.credentialSecret.name=member"]
BASE += ["--set", "workers.credentialSecret.key=credential"]
HOST = {
    "workers": {
        "homeVolume": {"emptyDir": None, "hostPath": {"path": "/mnt/data/hive", "type": "Directory"}},
        "nodeSelector": {"kubernetes.io/hostname": "data-node"},
        "tolerations": [{"key": "dedicated", "operator": "Exists", "effect": "NoSchedule"}],
        "maxAgents": 1,
        "worktreeMinFreeGb": 70,
        "memoryPerAgentMi": {"request": 512, "limit": 2048},
    }
}


def hive(text: str) -> dict:
    [found] = [
        d
        for d in yaml.safe_load_all(text)
        if d and d["kind"] == "Deployment" and d["metadata"]["name"].endswith("worker-hive")
    ]
    return found


def problems(deployment: dict, workers: dict | None) -> list[str]:
    spec = deployment["spec"]["template"]["spec"]
    home = next(v for v in spec["volumes"] if v["name"] == "home")
    strategy = deployment["spec"].get("strategy", {}).get("type")
    env = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
    floor = str((workers or {}).get("worktreeMinFreeGb", 50))
    found = (
        []
        if env.get("WT_MIN_FREE_GB") == floor
        else [f"WT_MIN_FREE_GB is {env.get('WT_MIN_FREE_GB')}, expected {floor}"]
    )
    if workers is None:
        if home != {"name": "home", "emptyDir": {}}:
            found.append(f"the default home volume is {home}, expected an emptyDir")
        if strategy is not None or "nodeSelector" in spec or "affinity" in spec or "tolerations" in spec:
            found.append("the default hive is pinned or rolls out by Recreate")
        return found
    if home != {"name": "home", "hostPath": workers["homeVolume"]["hostPath"]}:
        found.append(f"the home volume is {home}, expected the hostPath of workers.homeVolume")
    if strategy != "Recreate":
        found.append(f"a hive with a host home rolls out by {strategy}, expected Recreate")
    if spec.get("nodeSelector") != workers["nodeSelector"]:
        found.append("the hive does not take workers.nodeSelector")
    if spec.get("tolerations") != workers["tolerations"]:
        found.append("the hive does not take workers.tolerations")
    memory = spec["containers"][0]["resources"]["requests"]["memory"]
    if memory != "512Mi":
        found.append(f"one agent at 512 MiB requests {memory}")
    return found


def render(chart: str, values: str | None) -> dict:
    extra = ["-f", "-"] if values else []
    result = subprocess.run(
        ["helm", "template", "fixture", chart, *BASE, *extra],
        input=values,
        capture_output=True,
        text=True,
        check=True,
    )
    return hive(result.stdout)


def main(chart: str) -> list[str]:
    found = problems(render(chart, None), None)
    pinned = render(chart, yaml.safe_dump(HOST))
    found += problems(pinned, HOST["workers"])
    planted = yaml.safe_load(yaml.safe_dump(pinned))
    planted["spec"].pop("strategy", None)
    if not problems(planted, HOST["workers"]):
        found.append("the check passed a hive with no Recreate rollout")
    both = {"workers": {"homeVolume": {"hostPath": HOST["workers"]["homeVolume"]["hostPath"]}}}
    try:
        render(chart, yaml.safe_dump(both))
        found.append("the chart rendered a home volume with both an emptyDir and a hostPath")
    except subprocess.CalledProcessError as refused:
        if "exactly one volume source" not in refused.stderr:
            found.append(f"the chart refused two home sources with {refused.stderr.strip()!r}")
    return found


if __name__ == "__main__":
    found = main(sys.argv[1])
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        sys.exit(1)
    print("the chart rendered the hive home volume and placement")
