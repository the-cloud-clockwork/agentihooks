"""Which contract a handoff document is held to: the expert handoff for an expert, Handoff v2 for everyone else."""

from collections.abc import Callable
from typing import Any


def problems(store: Any, slug: str, agent: Any, text: str, resolves: Callable[[str], object]) -> list[str]:
    """The problems of a handoff document under the contract its author is held to."""
    raise NotImplementedError
