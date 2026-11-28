from typing import Any

from scripts.swarm_ledger.api import resources


def under_the_export_message(reply: dict[str, Any]) -> dict[str, Any]:
    # ruleid: feature-api-read-names-the-read-that-fits
    return resources.bounded(reply)


def a_choice_that_falls_back_to_it(reply: dict[str, Any], too_large: str) -> dict[str, Any]:
    # ruleid: feature-api-read-names-the-read-that-fits
    return resources.bounded(reply, too_large) if too_large else resources.bounded(reply)


def with_its_own_message(reply: dict[str, Any]) -> dict[str, Any]:
    # ok: feature-api-read-names-the-read-that-fits
    return resources.bounded(reply, "The rows pass the reply limit: read them in pages with the list read")


def handle(result: dict[str, Any]) -> dict[str, Any]:
    # ok: feature-api-read-names-the-read-that-fits
    return resources.bounded(result)
