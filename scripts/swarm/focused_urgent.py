"""The open, urgent task inside the open focus that a pull request serves, shared by the master seat's queue routes."""

from typing import Any


def focused(doc: dict[str, Any], task_id: str) -> bool:
    """Whether the task sits inside the open focus, or no focus has open work."""
    from scripts.swarm import freeze
    from scripts.swarm_ledger.repository import hierarchy

    admitted = freeze.admitted(doc, hierarchy.project(doc)[0])  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    return admitted is None or task_id in admitted


def task(slug: str, url: str, ledger: Any) -> str | None:
    """The first open, urgent task naming ``url`` that sits inside the open focus, else None."""
    rows: list[dict[str, Any]] = ledger.tasks_targeting(slug, url)
    urgent = [row for row in rows if row.get("rank") == "urgent" and row.get("state") != "done"]
    if not urgent:
        return None
    from scripts.swarm.ledger_client import pass_state

    doc = pass_state(ledger, slug)
    return next((row["id"] for row in urgent if focused(doc, row["id"])), None)
