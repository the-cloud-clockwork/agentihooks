from collections.abc import Callable
from typing import Any


def repeated_victim(
    _victim: Callable[..., Any],
    holders: list[Any],
    asked: dict[str, str],
    rows: dict[str, Any],
    doc: dict[str, Any],
    graph: Any,
) -> Any:
    # ruleid: preempt-repeated-victim-task
    return _victim([holder for holder in holders if holder.name not in asked], rows, doc, graph)


def rotated_victim(
    _victim: Callable[..., Any],
    holders: list[Any],
    asked: dict[str, str],
    recent: set[str],
    rows: dict[str, Any],
    doc: dict[str, Any],
    graph: Any,
) -> Any:
    # ok: preempt-repeated-victim-task
    return _victim(
        [holder for holder in holders if holder.name not in asked and holder.task not in recent], rows, doc, graph
    )
