from scripts.swarm import stack
from scripts.swarm.naming import plain


def fresh(reason: str) -> dict:
    return {"remote_branch": "none", "remote_head": "none", "continue_from": "fresh", "fresh_reason": reason}


def resolve(repo: str, task: dict, rows: dict, lives: list[str], envelope: dict, reclaim) -> dict:
    origin = stack.origin_key(repo) if task.get("branch_repo") else ""
    if task.get("branch_repo") and stack.repo_key(task["branch_repo"]) != origin:
        return fresh("the task branch belongs to another repository")
    forbidden = {
        row["branch"]
        for row in rows.values()
        if row["id"] != task["id"]
        and row.get("branch")
        and row.get("pr_url")
        and row["pr_url"] != task.get("pr_url")
        and stack.repo_key(row.get("branch_repo") or row["pr_url"].split("/pull/")[0])
        == (origin or stack.origin_key(repo))
    }
    recorded = task.get("branch") or ""
    branch = envelope.get("continue_from", "").removeprefix("origin/")
    allowed = {recorded} if recorded else {plain(name) for name in lives}
    if envelope and branch in allowed - forbidden and envelope.get("task", task["id"]) == task["id"]:
        return envelope
    options = {"forbidden": forbidden} if forbidden else {}
    return reclaim(repo, lives, recorded, **options)
