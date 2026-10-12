"""The launch ConfigMap's required volume holds the worker until it exists, so the hand may follow the Pod create."""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Protocol

from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.auth_context import GrantRefused, Registration
from scripts.swarm_v2.broadcast_bridge import GRANT_NAME
from scripts.swarm_v2.hand import HANDED, Hand
from scripts.swarm_v2.kubernetes.client import AlreadyExists, ApiRefused, PodApi, PreconditionFailed
from scripts.swarm_v2.kubernetes.runtime import GENERATION_LABEL
from scripts.swarm_v2.kubernetes.spec import LAUNCH_RECORD, launch_name, pod_name
from scripts.swarm_v2.kubernetes.watch import EXECUTION_LABEL, OWNER_LABEL, owner_for
from scripts.swarm_v2.supervision import AUTHORITY, SCHEMA_VERSION, Budgets


@dataclass(frozen=True)
class Supervision:
    harness: str
    exporter: tuple[str, ...] | None
    control_url: str
    budgets: Budgets = Budgets()
    checkpoint_remote: bool = False

    def record(self, registration: Registration, profile: str, restore: dict | None = None) -> str:
        record = {
            "schema_version": SCHEMA_VERSION,
            "execution_id": registration.execution_id,
            "generation": registration.generation,
            "authority": {name: getattr(registration, name) for name in AUTHORITY},
            "harness": self.harness,
            "agent": [self.harness],
            "control_url": self.control_url,
            "profile": profile,
            "registration": asdict(registration),
            **self.budgets.record(),
            "checkpoint_acknowledgement": {"status": "pending"},
        }
        if self.checkpoint_remote:
            record["checkpoint_remote"] = True
        if self.exporter:
            record["exporter"] = list(self.exporter)
        if restore is not None:
            record["restore"] = restore
        return json.dumps(record, sort_keys=True)


def _owned(pod: dict, labels: dict[str, str]) -> bool:
    metadata = pod["metadata"]
    found = metadata.get("labels", {})
    return "deletionTimestamp" not in metadata and {key: found.get(key) for key in labels} == labels


def _material(pod: dict, labels: dict[str, str], grant: str, record: str) -> dict:
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
        "data": {GRANT_NAME: grant, LAUNCH_RECORD: record},
    }


class Terminals(Protocol):
    def open(self, registration: Registration, pod: dict) -> str: ...


class PodGrants:
    def __init__(
        self,
        api: PodApi,
        slug: str,
        verify: Callable[[str], Registration],
        supervision: Supervision,
        terminal: Terminals | None = None,
        restores: Callable[[str], dict | None] = lambda execution_id: None,
    ) -> None:
        self.api, self.owner = api, owner_for(slug)
        self.verify, self.supervision, self.terminal = verify, supervision, terminal
        self.restores = restores

    def hand(self, agent: AgentRecord, grant: str) -> Hand:
        from redis.exceptions import RedisError

        try:
            registration = self.verify(grant)
        except (GrantRefused, RedisError):
            return Hand(False, "grant_unverified")
        record = self.supervision.record(registration, agent.profile, self.restores(agent.execution_id))
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
        containers = pod.get("spec", {}).get("containers", [])
        if containers and "@" in containers[0].get("image", ""):
            material = json.loads(record)
            material["image_digest"] = containers[0]["image"].split("@", 1)[1]
            record = json.dumps(material, sort_keys=True)
        try:
            self.api.create_config_map(_material(pod, labels, grant, record))
        except AlreadyExists:
            return Hand(False, "config_map_exists", self._remove(pod))
        except ApiRefused:
            return Hand(False, "config_map_refused", self._remove(pod))
        except OSError:
            return Hand(False, "config_map_unavailable", self._remove(pod))
        if self.terminal is not None and (refused := self.terminal.open(registration, pod)):
            return Hand(False, refused, self._remove(pod))
        return HANDED

    def _remove(self, pod: dict) -> bool:
        metadata = pod["metadata"]
        try:
            self.api.delete("pods", metadata["name"], metadata["uid"])
        except (PreconditionFailed, ApiRefused, OSError):
            return False
        return True
