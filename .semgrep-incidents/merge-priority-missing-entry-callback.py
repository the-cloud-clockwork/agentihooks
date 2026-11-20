from typing import Any


def watcher_bad(run: Any) -> dict:
    # ruleid: merge-priority-missing-entry-callback
    return run.queue("queue", "url", journal=None)


def watcher_good(run: Any) -> dict:
    # ok: merge-priority-missing-entry-callback
    return run.queue("queue", "url", journal=None, priority=lambda: None)


def command_bad(queue: Any) -> dict:
    # ruleid: merge-priority-missing-entry-callback
    return queue.operate("queue", "url", admit=lambda head: None)


def command_good(queue: Any) -> dict:
    # ok: merge-priority-missing-entry-callback
    return queue.operate("queue", "url", admit=lambda head: None, priority=lambda: None)
