"""The expert handoff: the stricter document an expert hands its lineage's seat off with."""

from collections.abc import Callable


def problems(text: str, resolves: Callable[[str], object], slug: str, mission: str) -> list[str]:
    """Every structural problem of an expert handoff for the lineage that works the given saved mission text."""
    raise NotImplementedError
