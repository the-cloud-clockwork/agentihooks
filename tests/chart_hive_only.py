import subprocess
import sys

import yaml

WORKERS = ["--set", "workers.replicas=1", "--set", "workers.credentialSecret.name=member"]
WORKERS += ["--set", "workers.credentialSecret.key=credential"]
SECRET = '[{"name": "member", "data": [{"secretKey": "credential", "remoteRef": {"key": "k8s/h", "property": "c"}}]}]'
HIVE = [
    ("ConfigMap", "-worktrees"),
    ("Deployment", "-worker-hive"),
    ("ExternalSecret", "member"),
]
SWARM = [("Deployment", "-controller"), ("StatefulSet", "-ledger"), ("StatefulSet", "-redis"), ("NetworkPolicy", "")]


def render(chart: str, *extra: str) -> list[tuple[str, str]]:
    argv = ["helm", "template", "hive", chart, *WORKERS, "--set-json", f"externalSecrets={SECRET}", *extra]
    text = subprocess.run(argv, capture_output=True, text=True, check=True).stdout
    return [(d["kind"], d["metadata"]["name"]) for d in yaml.safe_load_all(text) if d]


def matches(found: list[tuple[str, str]], kind: str, suffix: str) -> bool:
    return any(k == kind and name.endswith(suffix) for k, name in found)


def problems(chart: str) -> list[str]:
    alone = render(chart, "--set", "hiveOnly=true", "--set", "hive.join.enabled=true", "--set", "ingress.enabled=true")
    found = [
        f"a hive only release renders no {kind} {suffix}" for kind, suffix in HIVE if not matches(alone, kind, suffix)
    ]
    extra = [item for item in alone if not any(item[0] == kind and item[1].endswith(suffix) for kind, suffix in HIVE)]
    found += [f"a hive only release renders {kind} {name}" for kind, name in extra]
    swarm = render(chart)
    found += [
        f"the default release lost its {kind} {suffix}" for kind, suffix in SWARM if not matches(swarm, kind, suffix)
    ]
    return found


if __name__ == "__main__":
    failures = problems(sys.argv[1])
    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        sys.exit(1)
    print("a hive only release renders only the worker hive; the default release keeps the swarm")
