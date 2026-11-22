"""Order times: when the operator's order reached each open task of a ledger."""

from typing import Any


def marked(row: dict[str, Any]) -> int | None:
    """The time of the row's order mark, None when it carries no well formed one."""
    return None


def stamps(doc: dict[str, Any]) -> dict[str, int]:
    """Each open task's order time."""
    return {}
