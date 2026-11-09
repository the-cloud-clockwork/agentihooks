from typing import Any

from scripts.swarm.health import spawn_stall, step_starvation


def findings_without_starvation(store: Any, slug: str, at: int) -> list[Any]:
    found = (
        list[Any]()
        # ruleid: status-without-step-starvation
        + spawn_stall.findings(store, slug)
    )
    return found


def findings_with_starvation(store: Any, slug: str, at: int) -> list[Any]:
    found = (
        list[Any]()
        # ok: status-without-step-starvation
        + spawn_stall.findings(store, slug)
        + step_starvation.findings(store, slug, at)
    )
    return found
