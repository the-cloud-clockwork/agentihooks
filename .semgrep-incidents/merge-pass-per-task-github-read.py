from typing import Any, Callable


def per_task_loop() -> Callable[[Any, Any], list[Any]]:
    # ruleid: merge-pass-per-task-github-read
    def merge_pass(run: Any, rows: Any) -> list[Any]:
        actions = []
        for task_id, row in rows.items():
            pull = run.github(row["pr_url"])
            actions.append(pull)
        return actions

    return merge_pass


def prefetched_first() -> Callable[[Any, Any], list[Any]]:
    # ok: merge-pass-per-task-github-read
    def merge_pass(run: Any, rows: Any) -> list[Any]:
        run = _prefetched(run, rows)
        actions = []
        for task_id, row in rows.items():
            pull = run.github(row["pr_url"])
            actions.append(pull)
        return actions

    return merge_pass
