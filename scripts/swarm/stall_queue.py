"""The master seat queues a focused, urgent pull request while a spawn stall finding is open, so a launch fix reaches
the merge queue when no lane engineer can launch to queue it."""

from typing import Any

from scripts.swarm import focused_urgent

ROUTE = "spawn stall"


def route(store: Any, slug: str, url: str, ledger: Any) -> dict[str, str] | None:
    """The queue journal context naming the stall route and its task when the master may queue ``url``, else None."""
    from scripts.swarm.health import spawn_stall

    if not spawn_stall.findings(store, slug):  # pyright: ignore[reportUnknownMemberType]
        return None
    task = focused_urgent.task(slug, url, ledger)
    return {"route": ROUTE, "task": task} if task else None
