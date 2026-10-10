"""The launch ConfigMap's required volume holds the worker until it exists, so the hand may follow the Pod create."""

from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.broadcast_bridge import GRANT_NAME
from scripts.swarm_v2.kubernetes.client import AlreadyExists, ApiRefused, PodApi, PreconditionFailed
from scripts.swarm_v2.kubernetes.runtime import GENERATION_LABEL
from scripts.swarm_v2.kubernetes.spec import launch_name, pod_name
from scripts.swarm_v2.kubernetes.watch import EXECUTION_LABEL, OWNER_LABEL, owner_for
from scripts.swarm_v2.launch import HANDED, Hand


def _owned(pod: dict, labels: dict[str, str]) -> bool:
    metadata = pod["metadata"]
    found = metadata.get("labels", {})
    return "deletionTimestamp" not in metadata and {key: found.get(key) for key in labels} == labels


def _material(pod: dict, labels: dict[str, str], grant: str) -> dict:
    metadata = pod["metadata"]
    owner = {"apiVersion": "v1", "kind": "Pod", "name": metadata["name"], "uid": metadata["uid"]}
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": launch_name(labels[EXECUTION_LABEL]),
            "namespace": metadata["namespace"],
            "labels": labels,
            "ownerReferences": [owner],
        },
        "immutable": True,
        "data": {GRANT_NAME: grant},
    }


class PodGrants:
    def __init__(self, api: PodApi, slug: str) -> None:
        self.api, self.owner = api, owner_for(slug)

    def hand(self, agent: AgentRecord, grant: str) -> Hand:
        labels = {
            OWNER_LABEL: self.owner,
            EXECUTION_LABEL: agent.execution_id,
            GENERATION_LABEL: str(agent.generation),
        }
        try:
            pod = self.api.read_pod(pod_name(agent.execution_id))
        except (ApiRefused, OSError):
            return Hand(False, "pod_unreadable")
        if pod is None:
            return Hand(False, "pod_missing")
        if not _owned(pod, labels):
            return Hand(False, "pod_foreign")
        try:
            self.api.create_config_map(_material(pod, labels, grant))
        except AlreadyExists:
            return Hand(False, "config_map_exists", self._remove(pod))
        except ApiRefused:
            return Hand(False, "config_map_refused", self._remove(pod))
        except OSError:
            return Hand(False, "config_map_unavailable", self._remove(pod))
        return HANDED

    def _remove(self, pod: dict) -> bool:
        metadata = pod["metadata"]
        try:
            return self.api.delete("pods", metadata["name"], metadata["uid"])
        except (PreconditionFailed, ApiRefused, OSError):
            return False
