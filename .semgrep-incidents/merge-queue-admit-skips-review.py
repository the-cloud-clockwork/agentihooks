from typing import Any


def queue_without_review(queue: Any, fresh: Any) -> dict[str, Any]:
    # ruleid: merge-queue-admit-skips-review
    return queue.operate("queue", "url", admit=lambda head: fresh("url", head), priority=lambda: None)


def queue_with_review(queue: Any, review: Any, fresh: Any) -> dict[str, Any]:
    # ok: merge-queue-admit-skips-review
    return queue.operate("queue", "url", admit=lambda head: review.admit("sw", head) or fresh("url", head))


def hotfix_skips_review(queue: Any, review: Any, hotfix: bool) -> dict[str, Any]:
    # ok: merge-queue-admit-skips-review
    return queue.operate("queue", "url", admit=lambda head: not hotfix and review.admit("sw", head))


def state_reads_without_admission(queue: Any) -> dict[str, Any]:
    # ok: merge-queue-admit-skips-review
    return queue.operate("state", "url", journal=None)
