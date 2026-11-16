"""The master seat queues a pull request a master sub agent opened for a task a master froze for it, so a master
built fix reaches the merge queue without unfreezing the task for a lane engineer to claim."""

from typing import Any

from scripts.swarm.store import MASTER

ROUTE = "master freeze"


def froze(records: list[dict[str, Any]], task_id: str) -> bool:
    """Whether a master seat froze this task by name."""
    target = f"tasks/{task_id}"
    return any(
        row["verb"] == "freeze" and row["target"] == target and row["by"].startswith(f"{MASTER}@") for row in records
    )


def route(slug: str, url: str, ledger: Any) -> dict[str, str] | None:
    """The queue journal context naming the freeze route and its task when the master may queue ``url``, else None."""
    from scripts.swarm import pull_head

    rows = [row for row in ledger.tasks_targeting(slug, url) if row.get("state") != "done"]
    records: list[dict[str, Any]] = ledger.freezes(slug)
    task = next((row for row in rows if froze(records, row["id"])), None)
    if task is None or not pull_head.pull_branch(url).branch.startswith(f"{MASTER}-"):
        return None
    return {"route": ROUTE, "task": task["id"]}
