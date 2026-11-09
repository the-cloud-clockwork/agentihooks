"""A hive seat that leaves a workstation task clears its claim, so a workstation seat can take the task."""

from collections.abc import Mapping
from typing import Any

from scripts.hive.scope import member


def fields(row: Mapping[str, Any], name: str) -> dict[str, str]:
    """The ledger fields that release ``name``'s claim on ``row`` when it leaves from a hive seat."""
    if not (member() and row.get("workstation") and row.get("claimed_by") == name):
        return {}
    return {"claimed_by": ""} if row.get("state") == "pr" else {"state": "open", "claimed_by": ""}


def release(ledger: Any, slug: str, row: Mapping[str, Any], name: str) -> dict[str, str]:
    """Clear ``name``'s claim on the workstation task ``row`` it leaves from a hive seat and return the fields written."""
    found = fields(row, name)
    if found:
        ledger.update_task(slug, row["id"], found, by=name, if_match={"claimed_by": name})
    return found
