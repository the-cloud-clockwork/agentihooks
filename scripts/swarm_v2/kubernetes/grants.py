"""The launch ConfigMap's required volume holds the worker until it exists, so the hand may follow the Pod create."""

from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.broadcast_bridge import GRANT_NAME
from scripts.swarm_v2.kubernetes.client import AlreadyExists, ApiRefused, PodApi
from scripts.swarm_v2.kubernetes.runtime import GENERATION_LABEL
from scripts.swarm_v2.kubernetes.spec import launch_name, pod_name
from scripts.swarm_v2.kubernetes.watch import EXECUTION_LABEL, OWNER_LABEL, owner_for


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

    def hand(self, agent: AgentRecord, grant: str) -> bool:
        labels = {
            OWNER_LABEL: self.owner,
            EXECUTION_LABEL: agent.execution_id,
            GENERATION_LABEL: str(agent.generation),
        }
        try:
            pod = self.api.read_pod(pod_name(agent.execution_id))
            if pod is None or not _owned(pod, labels):
                return False
            self.api.create_config_map(_material(pod, labels, grant))
        except (AlreadyExists, ApiRefused, OSError):
            return False
        return True
