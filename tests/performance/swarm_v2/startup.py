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
READY_FILE = "/tmp/startup-ready"
BOOT = f"python /opt/swarm-node/worker_image.py report > /tmp/report.json && touch {READY_FILE} && sleep 900"


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


def forget_image(node: str, image: str) -> None:
    run("docker", "exec", node, "crictl", "rmi", image)


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
    for node in nodes:
        forget_image(node, image)
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
    """The read only Anton listings the committed anton reads were reduced from."""
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


def kind(args: argparse.Namespace) -> None:
    try:
        outcome = scenario(args.image, args.nodes, args.grace, args.outage, args.output)
    finally:
        run("docker", "start", REGISTRY, check=False)
        kubectl("delete", "namespace", NAMESPACE, "--wait=false", check=False)
    (args.output / "fixture.json").write_text(json.dumps(outcome, indent=2, sort_keys=True) + "\n")
    print(json.dumps(outcome, sort_keys=True))
    interruption = outcome["interruption"]
    checks = [*outcome["ready"].values(), *interruption["classified_within_grace"].values()]
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
    args = parser.parse_args()
    if args.mode == "anton":
        read_anton(args.output)
    else:
        kind(args)


if __name__ == "__main__":
    main()
