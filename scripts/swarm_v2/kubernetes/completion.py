"""Worker Pods run with restartPolicy Never: once a Pod has Succeeded its archive watermark cannot advance."""

from collections import Counter
from collections.abc import Iterable

from scripts.swarm.store import RedisStore
from scripts.swarm_v2 import retention
from scripts.swarm_v2.kubernetes.cleanup import Cleanup
from scripts.swarm_v2.kubernetes.client import ApiRefused
from scripts.swarm_v2.kubernetes.failures import FENCES
from scripts.swarm_v2.kubernetes.runtime import GENERATION_LABEL
from scripts.swarm_v2.kubernetes.watch import BACKEND, EXECUTION_LABEL

SUCCEEDED, COMPLETED, REFUSED = "Succeeded", "completed", "refused"


class Completion:
    def __init__(self, store: RedisStore, slug: str, cleanup: Cleanup) -> None:
        self.store, self.slug, self.cleanup = store, slug, cleanup

    def __call__(self, pods: Iterable[dict]) -> dict[str, str]:
        pods = list(pods)
        known = {
            agent.execution_id: agent.generation
            for agent in self.store.execution_registry.records(self.slug)
            if agent.runtime_backend == BACKEND
        }
        listed = Counter(pod["metadata"]["labels"].get(EXECUTION_LABEL) for pod in pods)
        for pod in pods:
            if self._completed(pod, known, listed):
                self._finalize(pod["metadata"])
        finals = set(self.store.redis.hkeys(self.store.key(self.slug, "attempt-finals")))
        finals -= set(self.store.redis.hkeys(self.store.key(self.slug, "attempt-retained")))
        return {execution_id: self._clean(execution_id) for execution_id in sorted(finals)}

    def _completed(self, pod: dict, known: dict[str, int], listed: Counter) -> bool:
        labels = pod["metadata"]["labels"]
        execution_id = labels.get(EXECUTION_LABEL)
        return (
            pod["status"].get("phase") == SUCCEEDED
            and execution_id in known
            and listed[execution_id] == 1
            and labels.get(GENERATION_LABEL) == str(known[execution_id])
            and not self.store.redis.hexists(self.store.key(self.slug, FENCES), execution_id)
            and retention.final(self.store, self.slug, execution_id) is None
        )

    def _finalize(self, metadata: dict) -> None:
        execution_id = metadata["labels"][EXECUTION_LABEL]
        end = retention.archive_watermark(self.store, self.slug, execution_id)
        outcome = f"pods/{metadata['name']}/{metadata['uid']}"
        retention.finalize(self.store, self.slug, self.cleanup.require, execution_id, COMPLETED, end, outcome)

    def _clean(self, execution_id: str) -> str:
        try:
            return self.cleanup.run(execution_id).state
        except (ApiRefused, ConnectionError, TimeoutError) as error:
            print(f"controller: cleanup of {execution_id} failed with {type(error).__name__}; retrying")
            return REFUSED
