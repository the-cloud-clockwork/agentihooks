from typing import Any


def skipped_review(queue: Any, fresh: Any) -> None:
    # ruleid: merge-queue-admit-skips-review
    queue.operate("queue", "url", admit=lambda head: fresh("url", head))


def missing_review(queue: Any) -> None:
    # ruleid: merge-queue-admit-skips-review
    queue.operate("queue", "url")


def missing_dynamic_review(queue: Any, args: Any) -> None:
    if args.action == "queue":
        # ruleid: merge-queue-admit-skips-review
        queue.operate(args.action, args.url)


def hotfix_skips_review(queue: Any, review: Any, hotfix: bool) -> None:
    # ruleid: merge-queue-admit-skips-review
    queue.operate("queue", "url", admit=lambda head: not hotfix and review.queue_admit(head))


def first_entry(queue: Any, review: Any) -> None:
    # ok: merge-queue-admit-skips-review
    queue.operate("queue", "url", admit=lambda head: review.queue_admit(head))


def state_read(queue: Any) -> None:
    # ok: merge-queue-admit-skips-review
    queue.operate("state", "url")


def missing_ready_review(run: Any) -> None:
    # ruleid: merge-queue-admit-skips-review
    run.queue("queue", "url")


def _review(review: Any, author: Any, url: str, head: str) -> None:
    review.admit(author, url, head)


def ready_entry(run: Any, review: Any, author: Any) -> None:
    # ok: merge-queue-admit-skips-review
    run.queue("queue", "url", admit=lambda head: _review(review, author, "url", head))


def skipped_ready_helper(run: Any) -> None:
    # ruleid: merge-queue-admit-skips-review
    def _review(author: Any, url: str, head: str) -> None:
        pass

    run.queue("queue", "url", admit=lambda head: _review(run, "url", head))
