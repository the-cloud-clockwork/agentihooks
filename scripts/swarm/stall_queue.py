"""The master seat queues a focused, urgent pull request while a spawn stall finding is open, so a launch fix reaches
the merge queue when no lane engineer can launch to queue it."""

from typing import Any

ROUTE = "spawn stall"


def focused(doc: dict[str, Any], task_id: str) -> bool:
    """Whether the task sits inside the open focus, or no focus has open work."""
    from scripts.swarm import freeze
    from scripts.swarm_ledger.repository import hierarchy

    admitted = freeze.admitted(doc, hierarchy.project(doc)[0])  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    return admitted is None or task_id in admitted


def route(store: Any, slug: str, url: str, ledger: Any) -> dict[str, str] | None:
    """The queue journal context naming the stall route and its task when the master may queue ``url``, else None."""
    from scripts.swarm.health import spawn_stall

    if not spawn_stall.findings(store, slug):  # pyright: ignore[reportUnknownMemberType]
        return None
    rows: list[dict[str, Any]] = ledger.tasks_targeting(slug, url)
    urgent = [row for row in rows if row.get("rank") == "urgent" and row.get("state") != "done"]
    if not urgent:
        return None
    from scripts.swarm.ledger_client import pass_state

    doc = pass_state(ledger, slug)
    task = next((row for row in urgent if focused(doc, row["id"])), None)
    return {"route": ROUTE, "task": task["id"]} if task else None
