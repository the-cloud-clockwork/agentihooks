from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum

__all__ = ["Grade", "Priority", "Unit", "priority_of"]


class Priority(IntEnum):
    HOTFIX = 0
    PULL_REQUEST = 1
    QUEUE = 2
    PROMOTION = 3
    BACKFILL = 4


@dataclass(frozen=True)
class Unit:
    kind: str
    key: str
    members: tuple[str, ...]


@dataclass(frozen=True)
class Grade:
    repo: str
    head: str
    base: str
    units: tuple[Unit, ...]
    priority: Priority


def priority_of(event: Mapping[str, object]) -> Priority:
    if event.get("hotfix") is True:
        return Priority.HOTFIX
    name = str(event["event_name"])
    classes = {
        "pull_request": Priority.PULL_REQUEST,
        "merge_group": Priority.QUEUE,
        "push": Priority.PROMOTION,
        "backfill": Priority.BACKFILL,
    }
    if name not in classes:
        raise ValueError(f"Unknown lab event: {name}")
    return classes[name]
