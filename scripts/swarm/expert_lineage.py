"""The record of an expert lineage: one saved mission, one seat, and the state only code moves.

The master or the operator starts a lineage from a mission document. The mission text is saved once under its
digest, so every expert that ever occupies the lineage's seat reads the same words. A lineage is running until it
ends; this module is the only writer of the lineage records and of the saved missions.
"""

from dataclasses import dataclass, replace
from typing import Any

__all__ = ["Lineage", "get", "listed", "mission", "replace", "saved", "start", "stop"]


@dataclass(frozen=True)
class Lineage:
    """One lineage: its id is its seat label, its mission is the address of the saved text."""

    id: str
    seat: str
    mission: str
    title: str
    source: str
    effort: str
    state: str
    started_by: str
    started_at: int
    ended_by: str = ""
    ended_at: int = 0
    note: str = ""


def start(store: Any, slug: str, text: str, source: str, effort: str, by: str, at: int) -> Lineage:
    """Save the mission text and open a running lineage for it; a malformed mission or another effort is refused."""
    raise NotImplementedError


def get(store: Any, slug: str, name: str) -> Lineage:
    """The lineage of that id; an unknown id is refused."""
    raise NotImplementedError


def listed(store: Any, slug: str) -> list[Lineage]:
    """Every lineage of the swarm in the order started."""
    return []


def mission(store: Any, slug: str, lineage: Lineage) -> str:
    """The saved mission text of the lineage; a mission that is gone or no longer matches its address is refused."""
    raise NotImplementedError


def saved(store: Any, slug: str, digest: str) -> bool:
    """Whether the swarm holds a saved mission under that digest."""
    return False


def stop(store: Any, slug: str, name: str, by: str, note: str, at: int) -> Lineage:
    """End a running lineage by its starter's order; a note says why, and a lineage that already ended is refused."""
    raise NotImplementedError
