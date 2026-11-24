import json


class HolderScoped:
    def _pending(self, asked: dict[str, str], task_id: str, now_ms: int) -> bool:
        # ruleid: preempt-holder-scoped-suppression
        return any(target == task_id and now_ms - at < 600_000 for target, at in map(json.loads, asked.values()))


class TaskScoped:
    def _pending(self, episodes: dict[str, int], task_id: str, now_ms: int) -> bool:
        # ok: preempt-holder-scoped-suppression
        at = episodes.get(task_id)
        return at is not None and now_ms - at < 600_000
