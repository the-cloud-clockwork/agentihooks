"""An attempt completes when its current Pod succeeded: with restartPolicy Never no container of it runs again, so the
transcript archived by then is the whole transcript and the succeeded Pod is the outcome reference."""

from collections.abc import Iterable

from scripts.swarm.store import RedisStore
from scripts.swarm_v2 import retention
from scripts.swarm_v2.kubernetes.cleanup import Cleanup
from scripts.swarm_v2.kubernetes.failures import FENCES
from scripts.swarm_v2.kubernetes.runtime import GENERATION_LABEL
from scripts.swarm_v2.kubernetes.watch import BACKEND, EXECUTION_LABEL

SUCCEEDED = "Succeeded"
COMPLETED = "completed"


class Completion:
    def __init__(self, store: RedisStore, slug: str, cleanup: Cleanup) -> None:
        self.store, self.slug, self.cleanup = store, slug, cleanup

    def __call__(self, pods: Iterable[dict]) -> dict[str, str]:
        """Finalizes each attempt whose current Pod succeeded, then runs cleanup for every final attempt not retained."""
        current = {
            agent.execution_id: agent.generation
            for agent in self.store.execution_occupants(self.slug).values()
            if agent.runtime_backend == BACKEND
        }
        for pod in pods:
            if self._completed(pod, current):
                self._finalize(pod["metadata"])
        finals = set(self.store.redis.hkeys(self.store.key(self.slug, "attempt-finals")))
        finals -= set(self.store.redis.hkeys(self.store.key(self.slug, "attempt-retained")))
        return {execution_id: self.cleanup.run(execution_id).state for execution_id in sorted(finals)}

    def _completed(self, pod: dict, current: dict[str, int]) -> bool:
        labels = pod["metadata"].get("labels", {})
        execution_id = labels.get(EXECUTION_LABEL)
        return (
            pod.get("status", {}).get("phase") == SUCCEEDED
            and execution_id in current
            and labels.get(GENERATION_LABEL) == str(current[execution_id])
            and not self.store.redis.hexists(self.store.key(self.slug, FENCES), execution_id)
            and retention.final(self.store, self.slug, execution_id) is None
        )

    def _finalize(self, metadata: dict) -> None:
        execution_id = metadata["labels"][EXECUTION_LABEL]
        end = retention.archive_watermark(self.store, self.slug, execution_id)
        outcome = f"pods/{metadata['name']}/{metadata['uid']}"
        retention.finalize(self.store, self.slug, self.cleanup.require, execution_id, COMPLETED, end, outcome)
