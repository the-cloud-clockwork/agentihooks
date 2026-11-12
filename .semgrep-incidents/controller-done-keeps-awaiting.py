def stale_completion() -> dict[str, str]:
    # ruleid: controller-done-keeps-awaiting
    return {"state": "done", "pr_url": "pull-request"}


def cleared_completion() -> dict[str, str]:
    # ok: controller-done-keeps-awaiting
    return {"state": "done", "awaiting": "", "pr_url": "pull-request"}


def pending_completion() -> dict[str, str]:
    # ruleid: controller-done-keeps-awaiting
    return {"state": "done", "awaiting": "merge", "pr_url": "pull-request"}
