import subprocess
import sys
from pathlib import Path

import yaml

RELEASE = "swarm"
SWARM = [("Deployment", "-controller"), ("StatefulSet", "-ledger"), ("StatefulSet", "-redis"), ("Deployment", "-hub")]
HIVE = [("ConfigMap", "-worktrees"), ("Deployment", "-worker-hive")]


def render(chart: str, example: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", RELEASE, chart, "-f", str(example)], capture_output=True, text=True)


def has(docs: list[dict], kind: str, suffix: str) -> bool:
    return any(d["kind"] == kind and d["metadata"]["name"].endswith(suffix) for d in docs)


def bundled(docs: list[dict]) -> list[dict]:
    return [d for d in docs if d["metadata"].get("labels", {}).get("helm.sh/chart", "").startswith("nats-")]


def bus_url(docs: list[dict]) -> str | None:
    for d in docs:
        if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-hub"):
            env = {row["name"]: row.get("value") for row in d["spec"]["template"]["spec"]["containers"][0]["env"]}
            return env.get("AGENTIHOOKS_BUS_URL")
    return None


def swarm_problems(docs: list[dict]) -> list[str]:
    return [f"renders no {kind} {suffix}" for kind, suffix in SWARM if not has(docs, kind, suffix)]


def hub(docs: list[dict], values: dict) -> list[str]:
    found = swarm_problems(docs)
    nats = bundled(docs)
    if not any(d["kind"] == "StatefulSet" for d in nats):
        found.append("renders no bundled NATS StatefulSet")
    if not any(d["kind"] == "ConfigMap" and "jetstream" in str(d.get("data")) for d in nats):
        found.append("renders the bundled NATS without JetStream")
    if bus_url(docs) != f"nats://{RELEASE}-nats:4222":
        found.append(f"points the hub at {bus_url(docs)}, expected nats://{RELEASE}-nats:4222")
    return found


def hive_only(docs: list[dict], values: dict) -> list[str]:
    found = [f"renders no {kind} {suffix}" for kind, suffix in HIVE if not has(docs, kind, suffix)]
    extra = [d for d in docs if not any(has([d], kind, suffix) for kind, suffix in HIVE)]
    return found + [f"renders {d['kind']} {d['metadata']['name']}" for d in extra]


def external_nats(docs: list[dict], values: dict) -> list[str]:
    found = swarm_problems(docs)
    if bundled(docs):
        found.append("still renders the bundled NATS")
    url = values["hub"]["bus"]["url"]
    if bus_url(docs) != url:
        found.append(f"points the hub at {bus_url(docs)}, expected {url}")
    return found


CHECKS = {"hub.yaml": hub, "hive-only.yaml": hive_only, "external-nats.yaml": external_nats}


def problems(chart: str) -> list[str]:
    folder = Path(chart) / "examples"
    present = sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []
    found = [f"examples/{name} has no render check" for name in present if name not in CHECKS]
    for name, check in CHECKS.items():
        path = folder / name
        if not path.is_file():
            found.append(f"the chart has no examples/{name}")
            continue
        result = render(chart, path)
        if result.returncode:
            found.append(f"examples/{name} fails to render: {result.stderr.strip()}")
            continue
        docs = [d for d in yaml.safe_load_all(result.stdout) if d]
        found += [f"examples/{name} {line}" for line in check(docs, yaml.safe_load(path.read_text()))]
    return found


if __name__ == "__main__":
    failures = problems(sys.argv[1])
    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        sys.exit(1)
    print("the hub, hive only and external NATS examples each render the install they describe")
