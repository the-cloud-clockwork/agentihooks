import json
import subprocess
import sys
from pathlib import Path

import yaml

SCRATCH = {"swarm": "proof-323133-t1199-1", "agent": "engineer@323133-3110", "task": "t1199", "repo": "github.com/o/r"}
HIVE = ["--set", "workers.replicas=1", "--set", "workers.credentialSecret.name=m"]
HIVE += ["--set", "workers.credentialSecret.key=c"]
REFUSED = [
    ({"controller": {"scratchSwarms": [{**SCRATCH, "swarm": "rig-grade-swarm"}]}}, "proof-<code>-<task>-<n>"),
    ({"controller": {"scratchSwarms": [{**SCRATCH, "repo": ""}]}}, "needs swarm, agent, task and repo"),
    (
        {"controller": {"scratchSwarms": [SCRATCH], "hostSwarms": [SCRATCH["swarm"]]}},
        "must not be in controller.hostSwarms",
    ),
    ({"controller": {"scratchSwarms": [SCRATCH, SCRATCH]}}, "lists a swarm more than once"),
    ({"workers": {"masterSwarms": ["a,b"]}}, "each workers.masterSwarms entry must be a swarm slug"),
]
START = '"master-swarms=${HIVE_MASTER_SWARMS-}"'


def render(chart: str, values: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["helm", "template", "fx", chart, *HIVE, "-f", "-"],
        input=yaml.safe_dump(values),
        capture_output=True,
        text=True,
    )


def env(rendered: str, suffix: str, container: str) -> dict:
    documents = [item for item in yaml.safe_load_all(rendered) if item and item["kind"] == "Deployment"]
    pod = next(item for item in documents if item["metadata"]["name"].endswith(suffix))["spec"]["template"]["spec"]
    found = next(item for item in pod["containers"] if item["name"] == container)
    return {item["name"]: item.get("value") for item in found.get("env", [])}


def proof_placement_problems(chart: str) -> list[str]:
    values = yaml.safe_load((Path(chart) / "ci" / "kind-workers.yaml").read_text())
    controller = values["controller"]
    selected = SCRATCH["swarm"]
    controller["api"] = {
        "enabled": True,
        "swarm": selected,
        "signingKey": {"keyId": "fixture", "secretName": "fixture"},
    }
    controller["workers"]["kubernetesProofSwarm"] = selected
    rendered = render(chart, values)
    if rendered.returncode:
        return [f"the Kubernetes proof swarm fails to render: {rendered.stderr.strip()}"]
    found = []
    if env(rendered.stdout, "-controller", "controller").get("AGENTIHOOKS_CONTROL_KUBERNETES_PROOF") != selected:
        found.append("the Kubernetes proof swarm selector does not reach the controller")
    for selector in ("rig-grade-swarm", selected + "x"):
        controller["workers"]["kubernetesProofSwarm"] = selector
        refused = render(chart, values)
        if refused.returncode == 0 or "controller.workers.kubernetesProofSwarm must name" not in refused.stderr:
            found.append(f"the chart accepts an invalid Kubernetes proof swarm {selector!r}")
    controller["workers"]["kubernetesProofSwarm"] = selected
    for field, key in (("api", "enabled"), ("workers", "apiUrl")):
        original = controller[field][key]
        controller[field][key] = False if key == "enabled" else ""
        refused = render(chart, values)
        if refused.returncode == 0 or "controller.workers.kubernetesProofSwarm must name" not in refused.stderr:
            found.append(f"the chart accepts a Kubernetes proof swarm without {field}.{key}")
        controller[field][key] = original
    return found


def problems(chart: str) -> list[str]:
    found = []
    default = render(chart, {})
    if default.returncode:
        return [f"the default release fails to render: {default.stderr.strip()}"]
    found.extend(proof_placement_problems(chart))
    if "AGENTIHOOKS_CONTROL_KUBERNETES_PROOF" in env(default.stdout, "-controller", "controller"):
        found.append("the default release routes proof engineers to Kubernetes")
    if "AGENTIHOOKS_CONTROL_SCRATCH_SWARMS" in env(default.stdout, "-controller", "controller"):
        found.append("the default release sets scratch swarms on the controller")
    if env(default.stdout, "-worker-hive", "worker-hive").get("HIVE_MASTER_SWARMS") != "":
        found.append("the default hive names a master swarm")
    listed = render(
        chart, {"controller": {"scratchSwarms": [SCRATCH]}, "workers": {"masterSwarms": [SCRATCH["swarm"], "b"]}}
    )
    if listed.returncode:
        return [*found, f"a listed scratch swarm fails to render: {listed.stderr.strip()}"]
    setting = env(listed.stdout, "-controller", "controller").get("AGENTIHOOKS_CONTROL_SCRATCH_SWARMS")
    if setting is None or json.loads(setting) != [SCRATCH]:
        found.append(f"the controller reads scratch swarms {setting!r}")
    hive = env(listed.stdout, "-worker-hive", "worker-hive")
    if (hive.get("HIVE_MASTER_SWARMS"), hive.get("HIVE_EXCLUDED")) != (f"{SCRATCH['swarm']},b", "master"):
        found.append(
            f"the hive reads master swarms {hive.get('HIVE_MASTER_SWARMS')!r}, excluded {hive.get('HIVE_EXCLUDED')!r}"
        )
    for values, reason in REFUSED:
        refused = render(chart, values)
        if refused.returncode == 0 or reason not in refused.stderr:
            found.append(f"{json.dumps(values)} is not refused with {reason!r}: {refused.stderr.strip()}")
    if START not in (Path(chart).parents[1] / "worker" / "start.sh").read_text():
        found.append("the hive start does not register the charted master swarms")
    return found


if __name__ == "__main__":
    failures = problems(sys.argv[1])
    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        sys.exit(1)
    print("scratch swarms reach the controller and master swarms reach the hive")
