"""A hive seat that leaves a workstation task clears its claim, so a workstation seat can take the task."""

from collections.abc import Mapping
from typing import Any


def fields(row: Mapping[str, Any], name: str) -> dict[str, str]:
    """The ledger fields that release ``name``'s claim on ``row`` when it leaves from a hive seat."""
    return {}


def release(ledger: Any, slug: str, task_id: str, name: str) -> dict[str, str]:
    """Clear ``name``'s claim on a workstation task it leaves from a hive seat and return the fields written."""
    return {}
