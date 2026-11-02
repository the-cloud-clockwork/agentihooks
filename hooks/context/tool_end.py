"""A call the PreToolUse hook denies never runs, so no PostToolUse closes it: end it here for the stall clock."""

from collections.abc import Callable
from typing import Any


def closing_denied(
    handler: Callable[[dict[str, Any]], None],
    payload: dict[str, Any],
    blocked: type[BaseException],
    end: Callable[[dict[str, Any]], None],
) -> None:
    try:
        handler(payload)
    except blocked:
        end(payload)
        raise
