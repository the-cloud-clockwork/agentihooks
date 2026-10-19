import json
from typing import Protocol

from scripts.swarm.store import RedisStore
from scripts.swarm_v2.kubernetes.watch import EXECUTION_LABEL

LOG_READS = 3


class Logs(Protocol):
    def pod_logs(self, name: str, container: str) -> str: ...


def startup(pod: dict, api: Logs | None = None) -> dict | None:
    status = pod.get("status", {})
    containers = status.get("initContainerStatuses", []) + status.get("containerStatuses", [])
    terminated = [entry for entry in containers if "terminated" in entry.get("state", {})]
    if status.get("phase") != "Failed" and not any(
        entry["state"]["terminated"].get("exitCode", 0) != 0 for entry in terminated
    ):
        return None
    return {
        "phase": status.get("phase", ""),
        "reason": status.get("reason", ""),
        "containers": [
            {"name": entry["name"], "terminated": entry["state"]["terminated"]}
            | ({} if api is None else {"log": api.pod_logs(pod["metadata"]["name"], entry["name"])})
            for entry in terminated
        ],
    }


def retain(store: RedisStore, slug: str, pod: dict, captured: dict) -> None:
    metadata = pod["metadata"]
    record = {"pod": metadata["name"], "uid": metadata["uid"], "labels": metadata["labels"], "startup": captured}
    store.redis.hsetnx(
        store.key(slug, "attempt-startup-diagnostics"), metadata["labels"][EXECUTION_LABEL], json.dumps(record)
    )
