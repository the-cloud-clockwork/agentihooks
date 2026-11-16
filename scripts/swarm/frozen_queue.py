"""The master seat queues a pull request a master sub agent opened for a task a master froze for it, so a master
built fix reaches the merge queue without unfreezing the task for a lane engineer to claim."""

from typing import Any

ROUTE = "master freeze"


def route(slug: str, url: str, ledger: Any) -> dict[str, str] | None:
    """The queue journal context naming the freeze route and its task when the master may queue ``url``, else None."""
    return None
