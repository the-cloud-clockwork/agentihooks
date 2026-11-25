from typing import Any


def failed(self: Any, slug: str, request: dict[str, str]) -> bool:
    # ruleid: hub-capacity-refusal-steers-harness
    task_id, harness, reason = request["task"], request["harness"], request["reason"]
    self.store.note_launch_failure(slug, task_id, reason, harness)
    return True


class ClassifiedAdmission:
    store: Any

    def failed(self, slug: str, request: dict[str, str]) -> bool:
        # ok: hub-capacity-refusal-steers-harness
        task_id, harness, reason = request["task"], request["harness"], request["reason"]
        if "No account slot available" in reason or reason.startswith("account reservation is full"):
            harness = ""
        self.store.note_launch_failure(slug, task_id, reason, harness)
        return True
