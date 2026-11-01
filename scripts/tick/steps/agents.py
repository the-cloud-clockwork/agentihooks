"""The agent steps of the tick pass: reap, outages, strays, failed panes, orphans and session upkeep."""

from collections.abc import Callable
from typing import Any

__all__ = ["STEPS"]

Get = Callable[[str], Any]

STEPS: tuple[object, ...] = ()
