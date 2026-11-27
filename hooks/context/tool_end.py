"""A call the PreToolUse hook denies never runs, so no PostToolUse closes it: end it here for the stall clock."""

from collections.abc import Callable
from typing import Any

from hooks.common import log


def closing_denied(
    handler: Callable[[dict[str, Any]], None],
    payload: dict[str, Any],
    blocked: type[BaseException],
    end: Callable[[dict[str, Any]], None],
) -> None:
    try:
        handler(payload)
    except blocked as block:
        try:
            end(payload)
        except Exception as error:
            log("denied call end failed", {"error": str(error)})
            raise block from error
        raise
