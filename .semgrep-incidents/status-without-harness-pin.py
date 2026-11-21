from typing import Any

from scripts.swarm.health import harness_pin, spawn_stall


def findings_without_harness_pin(store: Any, slug: str, at: int) -> list[Any]:
    found = (
        list[Any]()
        # ruleid: status-without-harness-pin
        + spawn_stall.findings(store, slug)
    )
    return found


def findings_with_harness_pin(store: Any, slug: str, at: int) -> list[Any]:
    found = (
        list[Any]()
        # ok: status-without-harness-pin
        + spawn_stall.findings(store, slug)
        + harness_pin.findings(store, slug, at)
    )
    return found
