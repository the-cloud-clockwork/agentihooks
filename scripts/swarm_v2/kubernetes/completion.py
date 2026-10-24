import json
from collections import Counter
from collections.abc import Iterable

from scripts.swarm.store import RedisStore
from scripts.swarm_v2 import retention
from scripts.swarm_v2.kubernetes.cleanup import Cleanup
from scripts.swarm_v2.kubernetes.client import ApiRefused
from scripts.swarm_v2.kubernetes.failures import FENCES, FRESH, RECOVERIES, RESUME
from scripts.swarm_v2.kubernetes.runtime import GENERATION_LABEL
from scripts.swarm_v2.kubernetes.watch import BACKEND, EXECUTION_LABEL
from scripts.swarm_v2.worker import completion

SUCCEEDED, COMPLETED, CANCELLED, REFUSED = "Succeeded", "completed", "cancelled", "refused"


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
                self._finalize(pod)
        self._retire()
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

    def _finalize(self, pod: dict) -> None:
        metadata = pod["metadata"]
        execution_id = metadata["labels"][EXECUTION_LABEL]
        reports = [
            entry.get("state", {}).get("terminated", {})
            for entry in pod["status"].get("containerStatuses", [])
            if entry["name"] == "agent"
        ]
        if len(reports) != 1 or reports[0].get("exitCode") != 0:
            return
        end = completion.transcript_end(
            reports[0].get("message", ""), execution_id, int(metadata["labels"][GENERATION_LABEL])
        )
        if end is None:
            return
        outcome = f"pods/{metadata['name']}/{metadata['uid']}"
        retention.finalize(self.store, self.slug, self.cleanup.require, execution_id, COMPLETED, end, outcome)

    def _retire(self) -> None:
        decided = self.store.redis.hgetall(self.store.key(self.slug, RECOVERIES))
        for execution_id, raw in sorted(decided.items()):
            if json.loads(raw)["mode"] not in (FRESH, RESUME) or retention.final(self.store, self.slug, execution_id):
                continue
            fence = json.loads(self.store.redis.hget(self.store.key(self.slug, FENCES), execution_id))
            end, outcome = fence["archive_watermark"], f"{FENCES}/{execution_id}"
            retention.finalize(self.store, self.slug, self.cleanup.require, execution_id, CANCELLED, end, outcome)

    def _clean(self, execution_id: str) -> str:
        try:
            return self.cleanup.run(execution_id).state
        except (ApiRefused, ConnectionError, TimeoutError) as error:
            print(f"controller: cleanup of {execution_id} failed with {type(error).__name__}; retrying")
            return REFUSED
