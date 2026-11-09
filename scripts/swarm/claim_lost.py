"""Shared task ownership checks for worker exits, tool calls and tick retirement."""

from collections.abc import Mapping


def lost(name: str, row: Mapping[str, object] | None) -> bool:
    if not row:
        return False
    return row.get("claimed_by") != name
