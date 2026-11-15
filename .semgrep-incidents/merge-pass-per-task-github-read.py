def per_task_loop():
    # ruleid: merge-pass-per-task-github-read
    def merge_pass(run, rows):
        actions = []
        for task_id, row in rows.items():
            pull = run.github(row["pr_url"])
            actions.append(pull)
        return actions

    return merge_pass


def prefetched_first():
    # ok: merge-pass-per-task-github-read
    def merge_pass(run, rows):
        run = _prefetched(run, rows)
        actions = []
        for task_id, row in rows.items():
            pull = run.github(row["pr_url"])
            actions.append(pull)
        return actions

    return merge_pass
