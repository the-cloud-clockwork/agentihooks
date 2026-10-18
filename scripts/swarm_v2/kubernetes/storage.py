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
PRIVATE_ROOTS = ("/home/worker/attempts", "/tmp", "/var/run/swarm")  # NOSONAR: Pod paths compared, never opened
NATIVE = frozenset({".codex", ".claude", ".config", ".local", "herdr"})
PRIVATE_KINDS = frozenset({"emptyDir", "configMap", "secret", "projected", "downwardAPI", "ephemeral"})
SOURCE_READ_ONLY = frozenset({"persistentVolumeClaim", "nfs"})
HOME = re.compile(r"(/(home|Users|var/home)/[^/]+|/root|/mnt/[a-zA-Z]/Users/[^/]+)(/.+)?")
HOME_PARENTS = re.compile(r"/|/home|/Users|/var|/var/home|/mnt|/mnt/[a-zA-Z]|/mnt/[a-zA-Z]/Users")


class StorageRefused(ValueError):
    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def normal(path: str) -> str:
    return posixpath.normpath("/" + path.lstrip("/"))


def _within(path: str, root: str) -> bool:
    return PurePosixPath(path).is_relative_to(root)


def _runtime(path: str) -> bool:
    path = normal(path)
    if _within(POD_HOME, path) or any(_within(path, root) or _within(root, path) for root in PRIVATE_ROOTS):
        return True
    return not NATIVE.isdisjoint(PurePosixPath(path).parts)


def _kinds(volume: dict) -> dict:
    return {kind: source for kind, source in volume.items() if kind != "name"}


def _private(volumes: list[dict]) -> bool:
    return bool(volumes) and all(_kinds(volume) and PRIVATE_KINDS.issuperset(_kinds(volume)) for volume in volumes)


def _read_only_source(volumes: list[dict]) -> bool:
    return bool(volumes) and all(
        _kinds(volume)
        and all(kind in SOURCE_READ_ONLY and source.get("readOnly") is True for kind, source in _kinds(volume).items())
        for volume in volumes
    )


def _operator_home(path: str) -> bool:
    path = normal(path)
    return HOME.fullmatch(path) is not None or HOME_PARENTS.fullmatch(path) is not None


def _scoped(mount: dict, execution: str | None) -> bool:
    if not execution or "subPath" not in mount:
        return False
    sub_path = mount["subPath"]
    return ".." not in sub_path.split("/") and _within(sub_path, execution)


def _containers(pod: dict) -> list[dict]:
    return [*pod["spec"].get("initContainers", []), *pod["spec"]["containers"]]


class MountChecker:
    def __init__(self) -> None:
        self.rejections = Counter()

    def _refuse(self, message: str, reason: str) -> None:
        self.rejections[reason] += 1
        raise StorageRefused(message, reason)

    def _check_mount(self, container: str, mount: dict, volumes: list[dict], execution: str | None) -> None:
        if _private(volumes) or mount.get("readOnly") is True or _read_only_source(volumes):
            return
        where = f"container {container} mounts shared volume {mount['name']} writable at {mount['mountPath']}"
        if _runtime(mount["mountPath"]):
            self._refuse(f"{where}, over a native runtime root that stays private to the attempt", "shared_runtime")
        if not _scoped(mount, execution):
            self._refuse(f"{where} without a subPath of its execution", "unscoped_shared_write")

    def check(self, pod: dict) -> None:
        volumes = pod["spec"].get("volumes", [])
        for volume in volumes:
            host = _kinds(volume).get("hostPath")
            if host is not None and _operator_home(host["path"]):
                self._refuse(
                    f"volume {volume['name']} mounts the operator home from the node; "
                    "give workers private homes and a read only seed instead",
                    "operator_home",
                )
        execution = pod["metadata"].get("labels", {}).get(EXECUTION_LABEL)
        for container in _containers(pod):
            for mount in container.get("volumeMounts", []):
                named = [volume for volume in volumes if volume["name"] == mount["name"]]
                self._check_mount(container["name"], mount, named, execution)

    def shared_runtime_mount_rejections_total(self) -> dict[str, int]:
        return dict(self.rejections)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.swarm_v2.kubernetes.storage")
    parser.add_argument("pod")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 64
    try:
        pod = json.loads(Path(args.pod).read_text())
    except (OSError, ValueError):
        print(f"{args.pod} is not a readable JSON file", file=sys.stderr)
        return 1
    try:
        MountChecker().check(pod)
    except StorageRefused as refused:
        print(refused, file=sys.stderr)
        return 1
    except (TypeError, KeyError, AttributeError):
        print(f"{args.pod} is not a Pod manifest", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
