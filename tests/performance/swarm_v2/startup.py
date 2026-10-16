"""Drive the startup fixture in a throwaway kind cluster: the worker image uncached then cached on each worker node,
then pulled again while the registry is stopped and restarted. Writes a capture folder for startup_bench reduce."""

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

NAMESPACE = "startup-bench"
REGISTRY = "kind-registry"
WORKERS = "agentihooks-swarm-workers"
READY_FILE = "/tmp/startup-ready"
REMOVE_SECONDS = 120
BOOT = f"python /opt/swarm-node/worker_image.py report > /tmp/report.json && touch {READY_FILE} && sleep 900"
POOL = "oci-swarm"
STARTER = "oci-pool-starter-"
POOL_LABEL = "anton.io/node-pool"
WORKLOAD_LABEL = "anton.io/workload"
TOLERATIONS = [
    {"key": "anton.io/remote", "operator": "Equal", "value": "oci", "effect": "NoSchedule"},
    {"key": WORKLOAD_LABEL, "operator": "Equal", "value": "swarm", "effect": "NoSchedule"},
]
OUTAGE_REGISTRY = "127.0.0.1:5000"
REGISTRY_IMAGE = "mirror.gcr.io/library/registry:2"
UPSTREAM = "https://ghcr.io"
COLD_POD = "outage-cold"


def run(*command: str, check: bool = True) -> str:
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if check and done.returncode:
        raise SystemExit(f"{' '.join(command)} failed: {done.stderr.strip()}")
    return done.stdout


def kubectl(*args: str, check: bool = True) -> str:
    return run("kubectl", *args, check=check)


def pod(name: str, node: str, image: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": {"app": "startup-bench"}},
        "spec": {
            "nodeSelector": {"kubernetes.io/hostname": node},
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 0,
            "containers": [
                {
                    "name": "worker",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["/opt/swarm-node/entrypoint.sh", "bash", "-c", BOOT],
                    "readinessProbe": {"exec": {"command": ["test", "-f", READY_FILE]}, "periodSeconds": 1},
                }
            ],
        },
    }


def launch(name: str, node: str, image: str) -> None:
    subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(pod(name, node, image)), text=True, check=True)


def wait_ready(name: str, seconds: int) -> bool:
    done = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "wait", "--for=condition=Ready", f"pod/{name}", f"--timeout={seconds}s"],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode == 0


def wait_pull_failure(name: str, seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = kubectl(
            "-n", NAMESPACE, "get", "events", "--field-selector", f"involvedObject.name={name}", "-o", "json"
        )
        if any(e["reason"] in ("Failed", "BackOff") for e in json.loads(found)["items"]):
            return True
        time.sleep(1)
    return False


def remove_pods(names: list[str]) -> list[dict]:
    """Returns the removed Pods' last state, so their Ready times survive into the capture."""
    kept = json.loads(kubectl("-n", NAMESPACE, "get", "pod", *names, "-o", "json"))["items"]
    kubectl("-n", NAMESPACE, "delete", "pod", *names, "--wait=true")
    return kept


def forget_image(node: str, image: str) -> bool:
    command = ["docker", "exec", node, "crictl", "--timeout", f"{REMOVE_SECONDS}s", "rmi", image]
    try:
        done = subprocess.run(command, capture_output=True, text=True, check=False, timeout=REMOVE_SECONDS + 30)
    except subprocess.TimeoutExpired:
        print(f"removing {image} on {node} took over {REMOVE_SECONDS + 30} seconds")
        return False
    if done.returncode:
        print(f"removing {image} on {node} failed: {done.stderr.strip()}")
    return done.returncode == 0


def capture(output: Path, removed: list[dict]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "read_at").write_text(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")
    for name, args in (("nodes", ("get", "nodes")), ("events", ("get", "events", "-A"))):
        (output / f"{name}.json").write_text(kubectl(*args, "-o", "json"))
    pods = json.loads(kubectl("get", "pods", "-A", "-o", "json"))
    pods["items"] = removed + pods["items"]
    (output / "pods.json").write_text(json.dumps(pods))
    (output / "activities.json").write_text(json.dumps({"Activities": []}))
    (output / "instances.json").write_text(json.dumps({"Reservations": []}))


def scenario(image: str, nodes: list[str], grace: int, outage: int, output: Path) -> dict:
    """Each node pulls uncached then cached; every node then pulls with the registry stopped for `outage` seconds,
    and each Pod must start once the registry returns."""
    kubectl("create", "namespace", NAMESPACE)
    outcome = {"nodes": nodes, "ready": {}, "interruption": {"outage_seconds": outage}}
    for node in nodes:
        for phase in ("uncached", "cached"):
            name = f"{phase}-{node}"
            launch(name, node, image)
            outcome["ready"][name] = wait_ready(name, 300)
    removed = remove_pods([f"{phase}-{node}" for node in nodes for phase in ("uncached", "cached")])
    outcome["interruption"]["image_removed"] = {node: forget_image(node, image) for node in nodes}
    run("docker", "stop", REGISTRY)
    stopped = time.monotonic()
    names = [f"interrupted-{node}" for node in nodes]
    for name, node in zip(names, nodes, strict=True):
        launch(name, node, image)
    outcome["interruption"]["classified_within_grace"] = {name: wait_pull_failure(name, grace) for name in names}
    time.sleep(max(outage - (time.monotonic() - stopped), 0))
    run("docker", "start", REGISTRY)
    outcome["interruption"]["recovered"] = {name: wait_ready(name, 420) for name in names}
    capture(output, removed)
    return outcome


def read_anton(output: Path) -> None:
    """The read only Anton listings the committed anton reads were reduced from, and the agent log of every swarm
    worker Pod whose log can be read; a Pod without one, such as a Pending Pod, gets no log file."""
    output.mkdir(parents=True, exist_ok=True)
    (output / "read_at").write_text(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")
    for name in ("nodes", "events", "pods"):
        scope = ("-A",) if name != "nodes" else ()
        (output / f"{name}.json").write_text(kubectl("get", name, *scope, "-o", "json"))
    (output / "activities.json").write_text(
        run("aws", "autoscaling", "describe-scaling-activities", "--max-items", "200", "--output", "json")
    )
    groups = json.loads(run("aws", "autoscaling", "describe-auto-scaling-groups", "--output", "json"))
    ids = [i["InstanceId"] for group in groups["AutoScalingGroups"] for i in group["Instances"]]
    found = run("aws", "ec2", "describe-instances", "--instance-ids", *ids, "--output", "json") if ids else ""
    (output / "instances.json").write_text(found or json.dumps({"Reservations": []}))
    logs = output / "logs"
    logs.mkdir(exist_ok=True)
    for name in kubectl("-n", WORKERS, "get", "pods", "-o", "name").split():
        pod = name.removeprefix("pod/")
        done = subprocess.run(
            ["kubectl", "-n", WORKERS, "logs", pod, "-c", "agent", "--timestamps"],
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode:
            print(f"no agent log for {pod}: {done.stderr.strip()}")
            continue
        (logs / f"{WORKERS}_{pod}.log").write_text(done.stdout)


def swarm_pod(name: str, image: str) -> dict:
    """A worker Pod only an empty swarm pool's new node can take, so the pool starter has to scale it from zero."""
    spec = pod(name, "", image)
    spec["spec"]["nodeSelector"] = {POOL_LABEL: POOL}
    spec["spec"]["tolerations"] = TOLERATIONS
    return spec


def registry_pod(node: str) -> dict:
    """A pull through cache of the production registry on the node's loopback, which containerd reaches over plain
    HTTP; until it runs, every pull from it is refused."""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "outage-registry", "namespace": NAMESPACE, "labels": {"app": "startup-bench"}},
        "spec": {
            "nodeSelector": {"kubernetes.io/hostname": node},
            "tolerations": TOLERATIONS,
            "hostNetwork": True,
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 0,
            "containers": [
                {
                    "name": "registry",
                    "image": REGISTRY_IMAGE,
                    "env": [
                        {"name": "REGISTRY_HTTP_ADDR", "value": OUTAGE_REGISTRY},
                        {"name": "REGISTRY_PROXY_REMOTEURL", "value": UPSTREAM},
                    ],
                }
            ],
        },
    }


def apply(manifest: dict) -> None:
    subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(manifest), text=True, check=True)


def starter_lines(seen: set[str]) -> None:
    """Pool starter Jobs keep only their last few runs, so their timestamped logs are collected while waiting."""
    for name in kubectl("-n", "kube-system", "get", "pods", "-o", "name").split():
        if name.removeprefix("pod/").startswith(STARTER):
            seen.update(kubectl("-n", "kube-system", "logs", name, "--timestamps", check=False).splitlines())


def scheduled_node(name: str, seen: set[str], seconds: int) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        starter_lines(seen)
        node = kubectl("-n", NAMESPACE, "get", "pod", name, "-o", "jsonpath={.spec.nodeName}")
        if node:
            starter_lines(seen)
            return node
        time.sleep(15)
    return ""


def oci_instance(node: str) -> dict:
    """Only the instance id and creation time are read: the instance metadata carries the join credentials."""
    found = json.loads(kubectl("get", "node", node, "-o", "json"))
    query = 'data.{id:id,"time-created":"time-created"}'
    instance = run(
        "oci",
        "--profile",
        "ANTON",
        "compute",
        "instance",
        "get",
        "--instance-id",
        found["spec"]["providerID"],
        "--query",
        query,
    )
    return {"pool": found["metadata"]["labels"][WORKLOAD_LABEL], "instance": json.loads(instance)}


def capture_anton(output: Path, seen: set[str], node: str) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "read_at").write_text(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")
    for name, args in (("nodes", ("get", "nodes")), ("events", ("get", "events", "-n", NAMESPACE))):
        (output / f"{name}.json").write_text(kubectl(*args, "-o", "json"))
    (output / "pods.json").write_text(kubectl("-n", NAMESPACE, "get", "pods", "-o", "json"))
    (output / "activities.json").write_text(json.dumps({"Activities": [], "PoolStarter": sorted(seen)}))
    (output / "instances.json").write_text(json.dumps({"Reservations": [], "Oci": [oci_instance(node)]}))


def cold_outage(image: str, grace: int, outage: int, output: Path) -> dict:
    """One worker Pod scales the empty swarm pool from zero and pulls through a registry that is absent from its
    scheduling for `outage` seconds, then started on the same node; the Pod must start once it returns."""
    if kubectl("get", "nodes", "-l", f"{POOL_LABEL}={POOL}", "-o", "name").strip():
        raise SystemExit(f"the {POOL} pool has nodes; a scale from zero needs it empty")
    kubectl("create", "namespace", NAMESPACE)
    seen: set[str] = set()
    apply(swarm_pod(COLD_POD, image))
    node = scheduled_node(COLD_POD, seen, 1200)
    if not node:
        raise SystemExit(f"{COLD_POD} was not scheduled onto a new {POOL} node")
    stopped = time.monotonic()
    outcome = {"node": node, "outage_seconds": outage, "classified_within_grace": wait_pull_failure(COLD_POD, grace)}
    time.sleep(max(outage - (time.monotonic() - stopped), 0))
    apply(registry_pod(node))
    outcome["registry_ready"] = wait_ready("outage-registry", 300)
    outcome["recovered"] = wait_ready(COLD_POD, 600)
    starter_lines(seen)
    capture_anton(output, seen, node)
    return outcome


def anton_outage(args: argparse.Namespace) -> None:
    try:
        outcome = cold_outage(args.image, args.grace, args.outage, args.output)
    finally:
        kubectl("delete", "namespace", NAMESPACE, "--wait=false", check=False)
    (args.output / "fixture.json").write_text(json.dumps(outcome, indent=2, sort_keys=True) + "\n")
    print(json.dumps(outcome, sort_keys=True))
    if not all(outcome[key] for key in ("classified_within_grace", "registry_ready", "recovered")):
        raise SystemExit(
            "the cold Pod did not classify its pull failure in grace, or did not start once the registry returned"
        )


def kind(args: argparse.Namespace) -> None:
    try:
        outcome = scenario(args.image, args.nodes, args.grace, args.outage, args.output)
    finally:
        run("docker", "start", REGISTRY, check=False)
        kubectl("delete", "namespace", NAMESPACE, "--wait=false", check=False)
    (args.output / "fixture.json").write_text(json.dumps(outcome, indent=2, sort_keys=True) + "\n")
    print(json.dumps(outcome, sort_keys=True))
    interruption = outcome["interruption"]
    removed = interruption["image_removed"]
    classified = interruption["classified_within_grace"]
    checks = [*outcome["ready"].values(), *(classified[f"interrupted-{node}"] for node in removed if removed[node])]
    if not all([*checks, *interruption["recovered"].values()]):
        raise SystemExit("a fixture Pod did not become ready, classify its pull failure in grace, or recover")


def main() -> None:
    parser = argparse.ArgumentParser()
    modes = parser.add_subparsers(dest="mode", required=True)
    fixture = modes.add_parser("kind")
    fixture.add_argument("--image", required=True, help="the image reference the kind nodes pull")
    fixture.add_argument("--nodes", nargs="+", required=True)
    fixture.add_argument("--grace", type=int, default=120)
    fixture.add_argument("--outage", type=int, default=90)
    fixture.add_argument("--output", type=Path, required=True)
    anton = modes.add_parser("anton")
    anton.add_argument("--output", type=Path, required=True)
    cold = modes.add_parser("anton-outage")
    cold.add_argument("--image", default=f"{OUTAGE_REGISTRY}/the-cloud-clockwork/agentihooks-worker:dev")
    cold.add_argument("--grace", type=int, default=120)
    cold.add_argument("--outage", type=int, default=90)
    cold.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "anton":
        read_anton(args.output)
    elif args.mode == "anton-outage":
        anton_outage(args)
    else:
        kind(args)


if __name__ == "__main__":
    main()
