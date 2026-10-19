"""Run a worker resource suite, read node capacity, derive profiles and gate their promotion."""

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from scripts.gates.base import Who
from scripts.swarm_v2 import operator_auth
from scripts.swarm_v2 import resource_profiles as profiles
from scripts.swarm_v2 import resource_sampling as sampling

SUFFIXES = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "k": 1000,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
}
DONE_PHASES = ("Succeeded", "Failed")
PASSED_ENV = ("PATH", "LANG", "PLAYWRIGHT_BROWSERS_PATH")
VERSION_RUN = {"capture_output": True, "text": True, "timeout": 60}
TOOLS = {
    "python": [sys.executable, "--version"],
    "pytest": [sys.executable, "-m", "pytest", "--version"],
    "playwright": [sys.executable, "-m", "playwright", "--version"],
    "cc": ["cc", "--version"],
    "kernel": ["uname", "-r"],
}
REQUIRED = {
    "run": (("--suite", Path), ("--root", Path), ("--generation", int), ("--source", str), ("--output", Path)),
    "nodes": (("--nodes", Path), ("--pods", Path), ("--output", Path)),
    "derive": (("--measurements", Path), ("--nodes", Path), ("--output", Path)),
    "promote": (("--accepted", Path), ("--candidate", Path)),
    "rollback": (("--accepted", Path),),
}
OPTIONAL = {
    "nodes": (("--read-at", str, ""),),
    "derive": (("--markdown", Path, None), ("--min-repeats", int, profiles.Policy.min_repeats)),
    "promote": (("--requalified-by", str, ""), ("--swarm", str, "")),
}
KEEPS = ("promoted", "requalified", "replayed")
TIMEOUT = 600


def _amount(text: str) -> Decimal:
    for suffix, scale in SUFFIXES.items():
        if text.endswith(suffix):
            return Decimal(text[: -len(suffix)]) * scale
    return Decimal(text)


def mib(text: str) -> int:
    return int(_amount(text) / sampling.MIB)


def millis(text: str) -> int:
    return math.ceil(_amount(text) * 1000)


def _requests(container: Mapping) -> tuple[Decimal, int]:
    requested = container.get("resources", {}).get("requests", {})
    return _amount(requested.get("memory", "0")), millis(requested.get("cpu", "0"))


def _pod_requests(pod: Mapping) -> tuple[Decimal, int]:
    spec = pod["spec"]
    main = [_requests(c) for c in spec["containers"]]
    init = [_requests(c) for c in spec.get("initContainers", [])]
    memory = max([sum(m for m, _ in main), *(m for m, _ in init)])
    cpu = max([sum(c for _, c in main), *(c for _, c in init)])
    return memory, cpu


def nodes(listing: Mapping, pods: Mapping) -> list[dict]:
    """Each node's allocatable capacity and the requests of the Pods already scheduled on it."""
    used: dict[str, list] = {}
    for pod in pods["items"]:
        node = pod["spec"].get("nodeName")
        if not node or pod["status"]["phase"] in DONE_PHASES:
            continue
        memory, cpu = _pod_requests(pod)
        total = used.setdefault(node, [0, 0, 0])
        total[0] += memory
        total[1] += cpu
        total[2] += 1
    rows = []
    for item in listing["items"]:
        name, labels = item["metadata"]["name"], item["metadata"].get("labels", {})
        allocatable = item["status"]["allocatable"]
        memory, cpu, count = used.get(name, (0, 0, 0))
        rows.append(
            {
                "name": name,
                "pool": labels.get("anton.io/pool", ""),
                "instance_type": labels.get("node.kubernetes.io/instance-type", ""),
                "allocatable_memory_mib": mib(allocatable["memory"]),
                "allocatable_cpu_millis": millis(allocatable["cpu"]),
                "allocatable_ephemeral_mib": mib(allocatable.get("ephemeral-storage", "0")),
                "colocated_memory_mib": math.ceil(memory / sampling.MIB),
                "colocated_cpu_millis": cpu,
                "colocated_pods": count,
            }
        )
    return rows


def versions(tools: Mapping[str, Sequence[str]]) -> dict[str, str]:
    found = {}
    for name, command in tools.items():
        try:
            done = subprocess.run(list(command), **VERSION_RUN)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            found[name] = "absent"
            continue
        lines = [line.strip() for line in (done.stdout + done.stderr).splitlines() if line.strip()]
        found[name] = lines[0] if lines else "absent"
    return found


def meminfo(text: str) -> dict[str, int]:
    return {line.split(":")[0]: int(line.split()[1]) // 1024 for line in text.splitlines()}


def host() -> dict:
    memory = meminfo(Path("/proc/meminfo").read_text())
    return {
        "cpus": os.cpu_count(),
        "memory_total_mib": memory["MemTotal"],
        "memory_available_mib": memory["MemAvailable"],
        "load_average_1m": round(os.getloadavg()[0], 2),
        "kernel": platform.release(),
    }


def environment(workdir: Path, children: int) -> dict[str, str]:
    passed = {name: os.environ[name] for name in PASSED_ENV if name in os.environ}
    return {**passed, "HOME": str(workdir), "TMPDIR": str(workdir), "CHILDREN": str(children)}


def run(suite: Path, root: Path, generation: int, tools: Mapping[str, Sequence[str]], source: str) -> dict:
    """Measure every workload at every child count and repeat, each in a fresh workdir removed afterwards."""
    plan = json.loads(suite.read_text())
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    started = host()
    found = versions(tools)
    runs = []
    for workload in plan["workloads"]:
        found[f"workload:{workload['name']}"] = workload["version"]
        found[f"workload-command:{workload['name']}"] = hashlib.sha256(
            json.dumps([workload["command"], workload["children"]]).encode()
        ).hexdigest()
        for children in workload["children"]:
            for repeat in range(1, plan["repeats"] + 1):
                workdir = Path(tempfile.mkdtemp(prefix=f"{workload['name']}-", dir=root))
                values = {
                    "python": sys.executable,
                    "children": children,
                    "workloads": suite.resolve().parent,
                    "workdir": workdir,
                }
                command = [part.format(**values) for part in workload["command"]]
                env = environment(workdir, children)
                measured = sampling.measure(command, workdir, env, plan["interval"], workload.get("timeout", TIMEOUT))
                shutil.rmtree(workdir)
                runs.append(
                    {
                        "workload": workload["name"],
                        "class": workload["class"],
                        "children": children,
                        "repeat": repeat,
                        **measured,
                    }
                )
    return {
        "suite": plan["suite"],
        "generation": generation,
        "source": source,
        "host": {**started, "load_average_1m_after": round(os.getloadavg()[0], 2)},
        "tools": found,
        "runs": runs,
    }


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name, flags in REQUIRED.items():
        command = commands.add_parser(name)
        for flag, kind in flags:
            command.add_argument(flag, type=kind, required=True)
        for flag, kind, default in OPTIONAL.get(name, ()):
            command.add_argument(flag, type=kind, default=default)
    return parser


def _reviewer(slug: str) -> profiles.Principal | None:
    from scripts.swarm_ledger import storage

    repository = storage.default()

    who = Who.from_env()
    slug = slug or who.swarm
    if not slug:
        return None
    credential = operator_auth.credential(slug, repository.token, who)
    return operator_auth.authenticator(repository.token)(slug, credential)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "run":
        _write(args.output, run(args.suite, args.root, args.generation, TOOLS, args.source))
        return 0
    if args.command == "nodes":
        listing, pods = args.nodes.read_bytes(), args.pods.read_bytes()
        read_at = args.read_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        digests = {"nodes": hashlib.sha256(listing).hexdigest(), "pods": hashlib.sha256(pods).hexdigest()}
        _write(
            args.output, {"read_at": read_at, "inputs": digests, "nodes": nodes(json.loads(listing), json.loads(pods))}
        )
        return 0
    if args.command == "rollback":
        restored = profiles.rollback(args.accepted)
        print(json.dumps({"restored": restored}))
        return 0 if restored is not None else 1
    if args.command == "derive":
        policy = profiles.Policy(min_repeats=args.min_repeats)
        try:
            capacity = json.loads(args.nodes.read_text())["nodes"]
            report = profiles.derive(json.loads(args.measurements.read_text()), capacity, policy)
        except profiles.ClaimRefused as refused:
            print(f"refused: {refused}", file=sys.stderr)
            return 2
        _write(args.output, report)
        if args.markdown:
            args.markdown.write_text(profiles.markdown(report))
        return 0
    candidate = json.loads(args.candidate.read_text())
    reviewer = _reviewer(args.swarm) if args.requalified_by else None
    outcome = profiles.accept(args.accepted, candidate, profiles.Policy(), args.requalified_by, reviewer=reviewer)
    print(json.dumps({"decision": outcome["decision"], "changes": outcome["changes"]}))
    return 0 if outcome["decision"] in KEEPS else 1


if __name__ == "__main__":
    sys.exit(main())
