from typing import Any

PAGED = ("tasks", "phases", "followups", "alerts", "notifications", "slices", "freezes")


def build(parts: dict[str, Any], tasks: list[Any], holds: tuple[str, bool], limits: dict[str, Any]) -> dict[str, Any]:
    return {}
