"""Refuse Pods that share a writable native runtime home or the operator home; seeds and per execution writes pass."""

import argparse
import json
import posixpath
import re
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

from scripts.swarm_v2.kubernetes.watch import EXECUTION_LABEL

POD_HOME = "/home/worker"
PRIVATE_ROOTS = ("/home/worker/attempts", "/tmp", "/var/run/swarm")
NATIVE = frozenset({".codex", ".claude", ".config", ".local", "herdr"})
PRIVATE_KINDS = frozenset({"emptyDir", "configMap", "secret", "projected", "downwardAPI", "ephemeral"})
OPERATOR_HOME = re.compile(r"/|/home|/root|/Users|/(home|Users)/[^/]+")


class StorageRefused(ValueError):
    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _within(path: str, root: str) -> bool:
    return PurePosixPath(path).is_relative_to(root)


def _runtime(path: str) -> bool:
    if _within(POD_HOME, path) or any(_within(path, root) or _within(root, path) for root in PRIVATE_ROOTS):
        return True
    return not NATIVE.isdisjoint(PurePosixPath(path).parts)


def _source(volume: dict) -> tuple[str | None, dict]:
    kinds = [name for name in volume if name != "name"]
    return (kinds[0], volume[kinds[0]]) if kinds else (None, {})


def _operator_home(volume: dict) -> bool:
    kind, source = _source(volume)
    return kind == "hostPath" and OPERATOR_HOME.fullmatch(posixpath.normpath(source.get("path", ""))) is not None


def _scoped(mount: dict, execution: str | None) -> bool:
    sub_path = mount.get("subPath", "")
    return bool(execution) and ".." not in sub_path.split("/") and _within(sub_path, execution)


def _containers(pod: dict) -> list[dict]:
    spec = pod.get("spec", {})
    return [*spec.get("initContainers", []), *spec.get("containers", [])]


class MountChecker:
    def __init__(self) -> None:
        self.rejections = Counter()

    def _refuse(self, message: str, reason: str) -> None:
        self.rejections[reason] += 1
        raise StorageRefused(message, reason)

    def _check_mount(self, container: str, mount: dict, volume: dict, execution: str | None) -> None:
        kind, source = _source(volume)
        if kind in PRIVATE_KINDS or mount.get("readOnly") is True or source.get("readOnly") is True:
            return
        where = f"container {container} mounts shared volume {mount['name']} writable at {mount['mountPath']}"
        if _runtime(mount["mountPath"]):
            self._refuse(f"{where}, over a native runtime root that stays private to the attempt", "shared_runtime")
        if not _scoped(mount, execution):
            self._refuse(f"{where} without a subPath of its execution", "unscoped_shared_write")

    def check(self, pod: dict) -> None:
        volumes = {volume["name"]: volume for volume in pod.get("spec", {}).get("volumes", [])}
        for name, volume in volumes.items():
            if _operator_home(volume):
                self._refuse(
                    f"volume {name} mounts the operator home from the node; "
                    "give workers private homes and a read only seed instead",
                    "operator_home",
                )
        execution = pod.get("metadata", {}).get("labels", {}).get(EXECUTION_LABEL)
        for container in _containers(pod):
            for mount in container.get("volumeMounts", []):
                self._check_mount(container["name"], mount, volumes.get(mount["name"], {}), execution)

    def shared_runtime_mount_rejections_total(self) -> dict[str, int]:
        return dict(self.rejections)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.swarm_v2.kubernetes.storage")
    parser.add_argument("action", choices=("check",))
    parser.add_argument("pod")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 64
    try:
        MountChecker().check(json.loads(Path(args.pod).read_text()))
    except (OSError, ValueError) as refused:
        message = str(refused) if isinstance(refused, StorageRefused) else f"{args.pod} is not a readable JSON file"
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
