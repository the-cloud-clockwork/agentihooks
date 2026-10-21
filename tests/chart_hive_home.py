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
        "worktreeMaxPerRepo": 40,
        "worktreeStaleDays": 3,
        "memoryPerAgentMi": {"request": 512, "limit": 2048},
    }
}


def hive(text: str) -> dict:
    [found] = [
        d
        for d in yaml.safe_load_all(text)
        if d and d["kind"] == "Deployment" and d["metadata"]["name"].endswith("worker-hive")
    ]
    [settings] = [
        d["data"]
        for d in yaml.safe_load_all(text)
        if d and d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-worktrees")
    ]
    found["worktrees"] = settings
    return found


def worktree_problems(deployment: dict, workers: dict | None) -> list[str]:
    limits = workers or {}
    expected = {
        "WT_MIN_FREE_GB": str(limits.get("worktreeMinFreeGb", 50)),
        "WT_MAX_PER_REPO": str(limits.get("worktreeMaxPerRepo", 60)),
        "WT_STALE_DAYS": str(limits.get("worktreeStaleDays", 7)),
    }
    found = (
        [] if deployment["worktrees"] == expected else [f"worktrees are {deployment['worktrees']}, expected {expected}"]
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    sources = [item["configMapRef"]["name"] for item in container.get("envFrom", []) if "configMapRef" in item]
    if not any(name.endswith("-worktrees") for name in sources):
        found.append("the hive does not take its environment from the worktrees ConfigMap")
    return found


def problems(deployment: dict, workers: dict | None) -> list[str]:
    spec = deployment["spec"]["template"]["spec"]
    home = next(v for v in spec["volumes"] if v["name"] == "home")
    strategy = deployment["spec"].get("strategy", {}).get("type")
    found = worktree_problems(deployment, workers)
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


HIVES = {
    "workers": {
        "replicas": 1,
        "homeVolume": HOST["workers"]["homeVolume"],
        "nodeSelector": HOST["workers"]["nodeSelector"],
        "maxAgents": 1,
        "hives": [
            {
                "name": "pool-1",
                "maxAgents": 3,
                "credentialSecret": {"name": "member-pool-1", "key": "credential"},
                "homeVolume": {"emptyDir": {}},
                "nodeSelector": {"pool": "swarm"},
                "tolerations": [{"key": "pool", "operator": "Equal", "value": "swarm", "effect": "NoSchedule"}],
            },
            {"name": "pool-2", "credentialSecret": {"name": "member-pool-2", "key": "credential"}},
        ],
    }
}


def hive_pods(text: str) -> dict:
    documents = [d for d in yaml.safe_load_all(text) if d]
    return {
        d["metadata"]["name"].split("worker-hive", 1)[1]: d
        for d in documents
        if d["kind"] == "Deployment" and "-worker-hive" in d["metadata"]["name"]
    }


def hive_problems(name: str, deployment: dict, expected: dict) -> list[str]:
    spec = deployment["spec"]["template"]["spec"]
    container = spec["containers"][0]
    env = {e["name"]: e.get("value") for e in container["env"]}
    join = next(v for v in spec["volumes"] if v["name"] == "join")["secret"]["secretName"]
    home = next(v for v in spec["volumes"] if v["name"] == "home")
    component = deployment["spec"]["selector"]["matchLabels"]["app.kubernetes.io/component"]
    worktrees = [e["configMapRef"]["name"] for e in container["envFrom"] if "configMapRef" in e]
    found = []
    if component != f"worker-hive{name}":
        found.append(f"hive{name} selects component {component}")
    if join != expected["credential"]:
        found.append(f"hive{name} mounts credential {join}, expected {expected['credential']}")
    if env["HIVE_MAX_AGENTS"] != expected["agents"]:
        found.append(f"hive{name} runs {env['HIVE_MAX_AGENTS']} agents, expected {expected['agents']}")
    if container["resources"]["requests"]["memory"] != expected["memory"]:
        found.append(f"hive{name} requests {container['resources']['requests']['memory']}")
    if {k: v for k, v in home.items() if k != "name"} != expected["home"]:
        found.append(f"hive{name} home is {home}")
    if spec.get("nodeSelector") != expected["nodeSelector"]:
        found.append(f"hive{name} is placed by {spec.get('nodeSelector')}")
    if not any(w.endswith(f"-worktrees{name}") for w in worktrees):
        found.append(f"hive{name} does not read its own worktrees ConfigMap")
    return found


def hives_problems(chart: str) -> list[str]:
    result = subprocess.run(
        ["helm", "template", "fixture", chart, *BASE, "-f", "-"],
        input=yaml.safe_dump(HIVES),
        capture_output=True,
        text=True,
        check=True,
    )
    pods = hive_pods(result.stdout)
    host = HOST["workers"]
    expected = {
        "": {
            "credential": "member",
            "agents": "1",
            "memory": "700Mi",
            "home": {"hostPath": host["homeVolume"]["hostPath"]},
            "nodeSelector": host["nodeSelector"],
        },
        "-pool-1": {
            "credential": "member-pool-1",
            "agents": "3",
            "memory": "2100Mi",
            "home": {"emptyDir": {}},
            "nodeSelector": {"pool": "swarm"},
        },
        "-pool-2": {
            "credential": "member-pool-2",
            "agents": "1",
            "memory": "700Mi",
            "home": {"hostPath": host["homeVolume"]["hostPath"]},
            "nodeSelector": host["nodeSelector"],
        },
    }
    if sorted(pods) != sorted(expected):
        return [f"the chart rendered hives {sorted(pods)}, expected {sorted(expected)}"]
    found = [p for name, pod in pods.items() for p in hive_problems(name, pod, expected[name])]
    planted = yaml.safe_load(yaml.safe_dump(pods["-pool-1"]))
    planted["spec"]["template"]["spec"]["volumes"][0]["secret"]["secretName"] = "member"
    if not hive_problems("-pool-1", planted, expected["-pool-1"]):
        found.append("the check passed a hive that mounts the base credential")
    refusals = {
        "needs its own credentialSecret": [{"name": "pool-1", "credentialSecret": {"name": "member", "key": "c"}}],
        "appears twice": [
            {"name": "pool-1", "credentialSecret": {"name": "a", "key": "c"}},
            {"name": "pool-1", "credentialSecret": {"name": "b", "key": "c"}},
        ],
        "lowercase DNS label": [{"name": "Pool_1", "credentialSecret": {"name": "a", "key": "c"}}],
        "sets replicas": [{"name": "pool-1", "replicas": 0, "credentialSecret": {"name": "a", "key": "c"}}],
    }
    for reason, hives in refusals.items():
        refused = subprocess.run(
            ["helm", "template", "fixture", chart, *BASE, "-f", "-"],
            input=yaml.safe_dump({"workers": {"hives": hives}}),
            capture_output=True,
            text=True,
        )
        if refused.returncode == 0 or reason not in refused.stderr:
            found.append(f"the chart did not refuse hives that {reason}: {refused.stderr.strip()!r}")
    return found


def main(chart: str) -> list[str]:
    found = problems(render(chart, None), None)
    pinned = render(chart, yaml.safe_dump(HOST))
    found += problems(pinned, HOST["workers"])
    planted = yaml.safe_load(yaml.safe_dump(pinned))
    planted["spec"].pop("strategy", None)
    if not problems(planted, HOST["workers"]):
        found.append("the check passed a hive with no Recreate rollout")
    planted["spec"]["template"]["spec"]["containers"][0].pop("envFrom", None)
    if not worktree_problems(planted, HOST["workers"]):
        found.append("the check passed a hive that ignores the worktrees ConfigMap")
    both = {"workers": {"homeVolume": {"hostPath": HOST["workers"]["homeVolume"]["hostPath"]}}}
    try:
        render(chart, yaml.safe_dump(both))
        found.append("the chart rendered a home volume with both an emptyDir and a hostPath")
    except subprocess.CalledProcessError as refused:
        if "exactly one volume source" not in refused.stderr:
            found.append(f"the chart refused two home sources with {refused.stderr.strip()!r}")
    return found + hives_problems(chart)


if __name__ == "__main__":
    found = main(sys.argv[1])
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        sys.exit(1)
    print("the chart rendered the hive home volume and placement")
