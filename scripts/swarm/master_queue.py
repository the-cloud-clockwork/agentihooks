"""The routes by which the master seat queues a pull request though it claims no task: the spawn stall route for a
launch fix, the freeze route for a pull request a master sub agent opened for a task a master froze for it, and the
focused urgent route for the pull request of any open, urgent task inside the focus, on any head branch."""

from typing import Any

from scripts.swarm import focused_urgent, pull_head, stall_queue
from scripts.swarm.store import MASTER

ROUTE = "master freeze"
URGENT = "focused urgent"


def froze(records: list[dict[str, Any]], task_id: str) -> bool:
    """Whether a master seat froze this task by name."""
    target = f"tasks/{task_id}"
    return any(
        row["verb"] == "freeze" and row["target"] == target and row["by"].startswith(f"{MASTER}@") for row in records
    )


def frozen(slug: str, url: str, ledger: Any) -> dict[str, str] | None:
    """The queue journal context naming the freeze route and its task when a master sub agent's ``url`` serves a
    task a master froze, else None."""
    rows = [row for row in ledger.tasks_targeting(slug, url) if row.get("state") != "done"]
    if not rows:
        return None
    records: list[dict[str, Any]] = ledger.freezes(slug)
    task = next((row for row in rows if froze(records, row["id"])), None)
    if task is None or not pull_head.pull_branch(url).branch.startswith(f"{MASTER}-"):
        return None
    return {"route": ROUTE, "task": task["id"]}


def urgent(slug: str, url: str, ledger: Any) -> dict[str, str] | None:
    """The queue journal context naming the focused urgent route and its task when ``url`` serves an open, urgent task
    inside the open focus, on any head branch, else None."""
    task = focused_urgent.task(slug, url, ledger)
    return {"route": URGENT, "task": task} if task else None


def route(store: Any, slug: str, url: str, ledger: Any) -> dict[str, str] | None:
    """The queue journal context of the first route that lets the master seat queue ``url``, else None."""
    return stall_queue.route(store, slug, url, ledger) or frozen(slug, url, ledger) or urgent(slug, url, ledger)
